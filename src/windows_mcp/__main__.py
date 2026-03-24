from contextlib import asynccontextmanager
from windows_mcp.auth import AuthClient
from fastmcp import FastMCP
from dataclasses import dataclass, field
from textwrap import dedent
from enum import Enum
from typing import Any
import logging
import asyncio
import click
import os

logger = logging.getLogger(__name__)

desktop: Any | None = None
watchdog: Any | None = None
analytics: Any | None = None
screen_size: Any | None = None
_local_mcp: FastMCP | None = None

instructions = dedent("""
Windows MCP server provides tools to interact directly with the Windows desktop,
thus enabling to operate the desktop on the user's behalf.
""")


def _get_desktop():
    return desktop


def _get_analytics():
    return analytics


def _build_local_mcp() -> FastMCP:
    """Create the local desktop-control server only when local mode is used."""
    global _local_mcp

    if _local_mcp is not None:
        return _local_mcp

    from windows_mcp.analytics import PostHogAnalytics
    from windows_mcp.desktop.service import Desktop
    from windows_mcp.tools import register_all
    from windows_mcp.watchdog.service import WatchDog

    @asynccontextmanager
    async def lifespan(app: FastMCP):
        """Runs initialization code before the server starts and cleanup code after it shuts down."""
        global desktop, watchdog, analytics, screen_size

        if os.getenv("ANONYMIZED_TELEMETRY", "true").lower() != "false":
            analytics = PostHogAnalytics()
        desktop = Desktop()
        watchdog = WatchDog()
        screen_size = desktop.get_screen_size()
        watchdog.set_focus_callback(desktop.tree.on_focus_change)

        try:
            watchdog.start()
            await asyncio.sleep(1)  # Simulate startup latency
            yield
        finally:
            if watchdog:
                watchdog.stop()
            if analytics:
                await analytics.close()

    _local_mcp = FastMCP(name="windows-mcp", instructions=instructions, lifespan=lifespan)
    register_all(_local_mcp, get_desktop=_get_desktop, get_analytics=_get_analytics)
    return _local_mcp


def __getattr__(name: str):
    if name in {"state_tool", "screenshot_tool"}:
        _build_local_mcp()
        from windows_mcp.tools import snapshot

        tool = getattr(snapshot, name)
        if tool is None:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
        return getattr(tool, "fn", tool)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _get_remote_proxy_types():
    from fastmcp.client.transports import StreamableHttpTransport
    from fastmcp.server.providers.proxy import ProxyClient

    return StreamableHttpTransport, ProxyClient


@dataclass
class Config:
    mode: str
    sandbox_id: str = field(default='')
    api_key: str = field(default='')


class Transport(Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"
    def __str__(self):
        return self.value

class Mode(Enum):
    LOCAL = "local"
    REMOTE = "remote"
    def __str__(self):
        return self.value


def _run_local_mode(transport: str, host: str, port: int) -> None:
    local_mcp = _build_local_mcp()
    match transport:
        case Transport.STDIO.value:
            local_mcp.run(transport=Transport.STDIO.value, show_banner=False)
        case Transport.SSE.value | Transport.STREAMABLE_HTTP.value:
            local_mcp.run(transport=transport, host=host, port=port, show_banner=False)
        case _:
            raise ValueError(f"Invalid transport: {transport}")


def _run_remote_mode(config: Config, transport: str, host: str, port: int) -> None:
    if not config.sandbox_id:
        raise ValueError("SANDBOX_ID is required for MODE: remote")
    if not config.api_key:
        raise ValueError("API_KEY is required for MODE: remote")

    client = AuthClient(api_key=config.api_key, sandbox_id=config.sandbox_id)
    client.authenticate()
    streamable_http_transport, proxy_client = _get_remote_proxy_types()
    backend = streamable_http_transport(url=client.proxy_url, headers=client.proxy_headers)
    proxy_mcp = FastMCP.as_proxy(proxy_client(backend), name="windows-mcp")

    match transport:
        case Transport.STDIO.value:
            proxy_mcp.run(transport=Transport.STDIO.value, show_banner=False)
        case Transport.SSE.value | Transport.STREAMABLE_HTTP.value:
            proxy_mcp.run(transport=transport, host=host, port=port, show_banner=False)
        case _:
            raise ValueError(f"Invalid transport: {transport}")

@click.command()
@click.option(
    "--transport",
    help="The transport layer used by the MCP server.",
    type=click.Choice([Transport.STDIO.value,Transport.SSE.value,Transport.STREAMABLE_HTTP.value]),
    default='stdio'
)
@click.option(
    "--host",
    help="Host to bind the SSE/Streamable HTTP server.",
    default="localhost",
    type=str,
    show_default=True,
)
@click.option(
    "--port",
    help="Port to bind the SSE/Streamable HTTP server.",
    default=8000,
    type=int,
    show_default=True,
)

def main(transport, host, port):
    config = Config(
        mode=os.getenv("MODE", Mode.LOCAL.value).lower(),
        sandbox_id=os.getenv("SANDBOX_ID", ''),
        api_key=os.getenv("API_KEY", '')
    )
    match config.mode:
        case Mode.LOCAL.value:
            _run_local_mode(transport=transport, host=host, port=port)
        case Mode.REMOTE.value:
            _run_remote_mode(config=config, transport=transport, host=host, port=port)
        case _:
            raise ValueError(f"Invalid mode: {config.mode}")

if __name__ == "__main__":
    main()
