from contextlib import asynccontextmanager
from windows_mcp.auth import AuthClient
from windows_mcp.config import is_debug, enable_debug
from fastmcp import FastMCP
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
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


def _http_middleware() -> list:
    """Return ASGI middleware for HTTP transports including CORS and OPTIONS handling."""
    return [
        Middleware(OptionsMiddleware),
        Middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        ),
    ]


class OptionsMiddleware:
    """ASGI middleware that intercepts OPTIONS requests and returns 200 OK."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["method"] == "OPTIONS":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [
                        [b"content-length", b"0"],
                        [b"access-control-allow-origin", b"*"],
                        [b"access-control-allow-methods", b"*"],
                        [b"access-control-allow-headers", b"*"],
                    ],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": b"",
                }
            )
        else:
            await self.app(scope, receive, send)


def _add_cors_options_handlers(mcp: FastMCP) -> None:
    """Register custom route handlers for CORS OPTIONS requests."""
    # Note: Custom routes are registered via the http_app() method when
    # the app is created, so we don't need to do anything here with the new
    # OptionsMiddleware approach. The middleware is passed via run().
    pass


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
            logger.debug("Server started, entering main loop")
            yield
        finally:
            logger.debug("Shutting down: stopping watchdog and analytics")
            if watchdog:
                watchdog.stop()
            if analytics:
                await analytics.close()

    _local_mcp = FastMCP(name="windows-mcp", instructions=instructions, lifespan=lifespan)
    register_all(_local_mcp, get_desktop=_get_desktop, get_analytics=_get_analytics)
    _add_cors_options_handlers(_local_mcp)
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
            local_mcp.run(
                transport=transport,
                host=host,
                port=port,
                show_banner=False,
                middleware=_http_middleware(),
            )
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
    _add_cors_options_handlers(proxy_mcp)

    match transport:
        case Transport.STDIO.value:
            proxy_mcp.run(transport=Transport.STDIO.value, show_banner=False)
        case Transport.SSE.value | Transport.STREAMABLE_HTTP.value:
            proxy_mcp.run(
                transport=transport,
                host=host,
                port=port,
                show_banner=False,
                middleware=_http_middleware(),
            )
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
@click.option(
    "--debug",
    help="Enable debug mode to provide verbose logging for troubleshooting.",
    is_flag=True,
    default=False,
    show_default=True,
)
def main(transport, host, port, debug):
    if debug:
        enable_debug()

    if is_debug():
        logging.basicConfig(level=logging.DEBUG)

    config = Config(
        mode=os.getenv("MODE", Mode.LOCAL.value).lower(),
        sandbox_id=os.getenv("SANDBOX_ID", ''),
        api_key=os.getenv("API_KEY", '')
    )
    logger.debug("Starting windows-mcp (mode=%s, transport=%s)", config.mode, transport)
    try:
        match config.mode:
            case Mode.LOCAL.value:
                _run_local_mode(transport=transport, host=host, port=port)
            case Mode.REMOTE.value:
                _run_remote_mode(config=config, transport=transport, host=host, port=port)
            case _:
                raise ValueError(f"Invalid mode: {config.mode}")
        logger.debug("Server shut down normally")
    except Exception:
        logger.error("Server exiting due to unhandled exception", exc_info=True)
        raise


if __name__ == "__main__":
    main()
