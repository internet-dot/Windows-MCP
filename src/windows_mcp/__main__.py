from windows_mcp.analytics import PostHogAnalytics, with_analytics
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import ProxyClient
from windows_mcp.desktop.service import Desktop, Size
from windows_mcp.watchdog.service import WatchDog
from contextlib import asynccontextmanager
from fastmcp.utilities.types import Image
from dataclasses import dataclass, field
from windows_mcp.auth import AuthClient
from mcp.types import ToolAnnotations
from fastmcp import FastMCP, Context
from windows_mcp import filesystem
from dotenv import load_dotenv
from textwrap import dedent
from typing import Literal
from enum import Enum
import logging
import asyncio
import click
import time
import os
import io

logger = logging.getLogger(__name__)
load_dotenv()


def _snapshot_profile_enabled() -> bool:
    value = os.getenv("WINDOWS_MCP_PROFILE_SNAPSHOT", "")
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_bool(value: bool | str) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _capture_desktop_state(
    *,
    use_vision: bool,
    use_dom: bool,
    use_annotation: bool,
    use_ui_tree: bool,
    width_reference_line: int | None,
    height_reference_line: int | None,
    display: list[int] | None,
    tool_name: str,
):
    profile_enabled = _snapshot_profile_enabled()
    profile_started_at = time.perf_counter()
    stage_started_at = profile_started_at
    desktop_state_ms = 0.0
    metadata_render_ms = 0.0
    screenshot_encode_ms = 0.0

    if use_dom and not use_ui_tree:
        raise ValueError("use_dom=True requires use_ui_tree=True")

    display_indices = Desktop.parse_display_selection(display)

    grid_lines = None
    if width_reference_line and height_reference_line:
        grid_lines = (int(width_reference_line), int(height_reference_line))

    desktop_state = desktop.get_state(
        use_vision=use_vision,
        use_dom=use_dom,
        use_annotation=use_annotation,
        use_ui_tree=use_ui_tree,
        as_bytes=False,
        grid_lines=grid_lines,
        display_indices=display_indices,
        max_image_size=Size(width=MAX_IMAGE_WIDTH, height=MAX_IMAGE_HEIGHT),
    )
    if profile_enabled:
        desktop_state_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    interactive_elements = desktop_state.tree_state.interactive_elements_to_string()
    scrollable_elements = desktop_state.tree_state.scrollable_elements_to_string()
    windows = desktop_state.windows_to_string()
    active_window = desktop_state.active_window_to_string()
    active_desktop = desktop_state.active_desktop_to_string()
    all_desktops = desktop_state.desktops_to_string()
    if profile_enabled:
        metadata_render_ms = (time.perf_counter() - stage_started_at) * 1000
        stage_started_at = time.perf_counter()

    screenshot_bytes = None
    if use_vision and desktop_state.screenshot is not None:
        buffered = io.BytesIO()
        desktop_state.screenshot.save(buffered, format="PNG")
        screenshot_bytes = buffered.getvalue()
        buffered.close()
    if profile_enabled:
        screenshot_encode_ms = (time.perf_counter() - stage_started_at) * 1000
        logger.info(
            "%s profile: desktop_state_ms=%.1f metadata_render_ms=%.1f png_encode_ms=%.1f total_ms=%.1f use_vision=%s use_dom=%s use_ui_tree=%s use_annotation=%s display=%s",
            tool_name,
            desktop_state_ms,
            metadata_render_ms,
            screenshot_encode_ms,
            (time.perf_counter() - profile_started_at) * 1000,
            use_vision,
            use_dom,
            use_ui_tree,
            use_annotation,
            display,
        )

    return {
        "desktop_state": desktop_state,
        "interactive_elements": interactive_elements,
        "scrollable_elements": scrollable_elements,
        "windows": windows,
        "active_window": active_window,
        "active_desktop": active_desktop,
        "all_desktops": all_desktops,
        "screenshot_bytes": screenshot_bytes,
    }


def _build_snapshot_response(
    capture_result: dict[str, object],
    *,
    include_ui_details: bool,
    ui_detail_note: str | None = None,
):
    desktop_state = capture_result["desktop_state"]
    interactive_elements = capture_result["interactive_elements"]
    scrollable_elements = capture_result["scrollable_elements"]
    windows = capture_result["windows"]
    active_window = capture_result["active_window"]
    active_desktop = capture_result["active_desktop"]
    all_desktops = capture_result["all_desktops"]
    screenshot_bytes = capture_result["screenshot_bytes"]

    metadata_text = f"Cursor Position: {desktop_state.cursor_position}\n"
    if desktop_state.screenshot_size:
        metadata_text += f"Screenshot Resolution: {desktop_state.screenshot_size.to_string()}\n"
    if desktop_state.screenshot_region:
        metadata_text += (
            f"Screenshot Region: {desktop_state.screenshot_region.xyxy_to_string()}\n"
        )
    if desktop_state.screenshot_displays:
        metadata_text += f"Displays: {','.join(str(index) for index in desktop_state.screenshot_displays)}\n"
        metadata_text += "Coordinate Space: Virtual desktop coordinates\n"
    if ui_detail_note:
        metadata_text += f"{ui_detail_note}\n"

    response_text = dedent(f'''
    {metadata_text}
    Active Desktop:
    {active_desktop}

    All Desktops:
    {all_desktops}

    Focused Window:
    {active_window}

    Opened Windows:
    {windows}
    ''')
    if include_ui_details:
        response_text += dedent(f'''

    List of Interactive Elements:
    {interactive_elements or "No interactive elements found."}

    List of Scrollable Elements:
    {scrollable_elements or 'No scrollable elements found.'}''')

    response = [response_text]
    if screenshot_bytes:
        response.append(Image(data=screenshot_bytes, format='png'))
    return response

@dataclass
class Config:
    mode: str
    sandbox_id: str = field(default='')
    api_key: str = field(default='')

MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT = 1920, 1080

desktop: Desktop | None = None
watchdog: WatchDog | None = None
analytics: PostHogAnalytics | None = None
screen_size: Size | None = None

instructions = dedent("""
Windows MCP server provides tools to interact directly with the Windows desktop,
thus enabling to operate the desktop on the user's behalf.
""")


@asynccontextmanager
async def lifespan(app: FastMCP):
    """Runs initialization code before the server starts and cleanup code after it shuts down."""
    global desktop, watchdog, analytics,screen_size

    # Initialize components here instead of at module level
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


mcp = FastMCP(name="windows-mcp", instructions=instructions, lifespan=lifespan)

@mcp.tool(
    name="App",
    description="Manages Windows applications with three modes: 'launch' (opens the prescibed application), 'resize' (adjusts the size/position of a named window or the active window if name is omitted), 'switch' (brings specific window into focus).",
    annotations=ToolAnnotations(
        title="App",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "App-Tool")
def app_tool(mode:Literal['launch','resize','switch']='launch',name:str|None=None,window_loc:list[int]|None=None,window_size:list[int]|None=None, ctx: Context = None):
    return desktop.app(mode,name,window_loc,window_size)

@mcp.tool(
    name="PowerShell",
    description="A comprehensive system tool for executing any PowerShell commands. Use it to navigate the file system, manage files and processes, and execute system-level operations. Capable of accessing web content (e.g., via Invoke-WebRequest), interacting with network resources, and performing complex administrative tasks. This tool provides full access to the underlying operating system capabilities, making it the primary interface for system automation, scripting, and deep system interaction.",
    annotations=ToolAnnotations(
        title="PowerShell",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
@with_analytics(analytics, "Powershell-Tool")
def powershell_tool(command: str, timeout: int = 30, ctx: Context = None) -> str:
    try:
        response, status_code = desktop.execute_command(command, timeout)
        return f"Response: {response}\nStatus Code: {status_code}"
    except Exception as e:
        return f"Error executing command: {str(e)}\nStatus Code: 1"


@mcp.tool(
    name='FileSystem',
    description="Manages file system operations with eight modes: 'read' (read text file contents with optional line offset/limit), 'write' (create or overwrite a file, set append=True to append), 'copy' (copy file or directory to destination), 'move' (move or rename file/directory), 'delete' (delete file or directory, set recursive=True for non-empty dirs), 'list' (list directory contents with optional pattern filter), 'search' (find files matching a glob pattern), 'info' (get file/directory metadata like size, dates, type). Relative paths are resolved from the user's Desktop folder. Use absolute paths to access other locations.",
    annotations=ToolAnnotations(
        title="FileSystem",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
    )
@with_analytics(analytics, "FileSystem-Tool")
def file_system_tool(
    mode: Literal['read', 'write', 'copy', 'move', 'delete', 'list', 'search', 'info'],
    path: str,
    destination: str | None = None,
    content: str | None = None,
    pattern: str | None = None,
    recursive: bool | str = False,
    append: bool | str = False,
    overwrite: bool | str = False,
    offset: int | None = None,
    limit: int | None = None,
    encoding: str = 'utf-8',
    show_hidden: bool | str = False,
    ctx: Context = None
) -> str:
    try:
        from platformdirs import user_desktop_dir
        default_dir = user_desktop_dir()
        if not os.path.isabs(path):
            path = os.path.join(default_dir, path)
        if destination and not os.path.isabs(destination):
            destination = os.path.join(default_dir, destination)

        recursive = recursive is True or (isinstance(recursive, str) and recursive.lower() == 'true')
        append = append is True or (isinstance(append, str) and append.lower() == 'true')
        overwrite = overwrite is True or (isinstance(overwrite, str) and overwrite.lower() == 'true')
        show_hidden = show_hidden is True or (isinstance(show_hidden, str) and show_hidden.lower() == 'true')

        match mode:
            case 'read':
                return filesystem.read_file(path, offset=offset, limit=limit, encoding=encoding)
            case 'write':
                if content is None:
                    return 'Error: content parameter is required for write mode.'
                return filesystem.write_file(path, content, append=append, encoding=encoding)
            case 'copy':
                if destination is None:
                    return 'Error: destination parameter is required for copy mode.'
                return filesystem.copy_path(path, destination, overwrite=overwrite)
            case 'move':
                if destination is None:
                    return 'Error: destination parameter is required for move mode.'
                return filesystem.move_path(path, destination, overwrite=overwrite)
            case 'delete':
                return filesystem.delete_path(path, recursive=recursive)
            case 'list':
                return filesystem.list_directory(path, pattern=pattern, recursive=recursive, show_hidden=show_hidden)
            case 'search':
                if pattern is None:
                    return 'Error: pattern parameter is required for search mode.'
                return filesystem.search_files(path, pattern, recursive=recursive)
            case 'info':
                return filesystem.get_file_info(path)
            case _:
                return f'Error: Unknown mode "{mode}". Use: read, write, copy, move, delete, list, search, info.'
    except Exception as e:
        return f'Error in File tool: {str(e)}'

@mcp.tool(
    name='Snapshot',
    description="Captures complete desktop state including: system language, focused/opened windows, interactive elements (buttons, text fields, links, menus with coordinates), and scrollable areas. Set use_vision=True to include screenshot with cursor highlight. Set use_annotation=False to get a clean screenshot without bounding box overlays on UI elements (default: True, draws colored rectangles around detected elements). Set use_ui_tree=False for a faster screenshot-only snapshot when you do not need interactive or scrollable element extraction. Set width_reference_lines/height_reference_lines to overlay a grid for better spatial reasoning (make sure vision is enabled to use it). Set use_dom=True for browser content to get web page elements instead of browser UI. Set display=[0] or display=[0,1] to limit all returned Snapshot information to specific screens; omit it to keep the default full-desktop behavior. Always call this first to understand the current desktop state before taking actions.",
    annotations=ToolAnnotations(
        title="Snapshot",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "State-Tool")
def state_tool(
    use_vision: bool | str = False,
    use_dom: bool | str = False,
    use_annotation: bool | str = True,
    use_ui_tree: bool | str = True,
    width_reference_line: int | None = None,
    height_reference_line: int | None = None,
    display: list[int] | None = None,
    ctx: Context = None
):
    try:
        capture_result = _capture_desktop_state(
            use_vision=_as_bool(use_vision),
            use_dom=_as_bool(use_dom),
            use_annotation=_as_bool(use_annotation),
            use_ui_tree=_as_bool(use_ui_tree),
            width_reference_line=width_reference_line,
            height_reference_line=height_reference_line,
            display=display,
            tool_name="Snapshot tool",
        )
    except Exception as e:
        logger.warning(
            "Snapshot failed with display=%s use_vision=%s use_dom=%s",
            display,
            use_vision if 'use_vision' in locals() else None,
            use_dom if 'use_dom' in locals() else None,
            exc_info=True,
        )
        return [f'Error capturing desktop state: {str(e)}. Please try again.']

    return _build_snapshot_response(capture_result, include_ui_details=True)


@mcp.tool(
    name='Screenshot',
    description="Captures a fast screenshot-first desktop snapshot with cursor position, desktop/window summaries, and an image. This path skips UI tree extraction for speed. Use Snapshot when you need interactive element ids, scrollable regions, or browser DOM extraction.",
    annotations=ToolAnnotations(
        title="Screenshot",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Screenshot-Tool")
def screenshot_tool(
    use_annotation: bool | str = False,
    width_reference_line: int | None = None,
    height_reference_line: int | None = None,
    display: list[int] | None = None,
    ctx: Context = None,
):
    try:
        capture_result = _capture_desktop_state(
            use_vision=True,
            use_dom=False,
            use_annotation=_as_bool(use_annotation),
            use_ui_tree=False,
            width_reference_line=width_reference_line,
            height_reference_line=height_reference_line,
            display=display,
            tool_name="Screenshot tool",
        )
    except Exception as e:
        logger.warning(
            "Screenshot failed with display=%s",
            display,
            exc_info=True,
        )
        return [f'Error capturing screenshot: {str(e)}. Please try again.']

    return _build_snapshot_response(
        capture_result,
        include_ui_details=False,
        ui_detail_note="UI Tree: Skipped for fast screenshot-only capture. Call Snapshot when you need interactive or scrollable elements.",
    )

@mcp.tool(
    name="Click",
    description=(
        "Performs mouse clicks at specified coordinates [x, y] or passing a UI element's label/id. "
        "Supports button types: 'left' for selection/activation, 'right' for context menus, 'middle'. "
        "Supports clicks: 0=hover only (no click), 1=single click (select/focus), 2=double click (open/activate). "
        "Provide either loc or label."
    ),
    annotations=ToolAnnotations(
        title="Click",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Click-Tool")
def click_tool(
    loc: list[int] | None = None,
    label: int | None = None,
    button: Literal["left", "right", "middle"] = "left",
    clicks: int = 1,
    ctx: Context = None,
) -> str:
    if loc is None and label is None:
        raise ValueError("Either loc or label must be provided.")
    if label is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        try:
            loc = list(desktop.get_coordinates_from_label(label))
        except Exception as e:
            raise ValueError(f"Failed to find element with label {label}: {e}")
    if len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    x, y = loc[0], loc[1]
    desktop.click(loc=loc, button=button, clicks=clicks)
    num_clicks = {0: "Hover", 1: "Single", 2: "Double"}
    return f"{num_clicks.get(clicks)} {button} clicked at ({x},{y})."


@mcp.tool(
    name="Type",
    description="Types text at specified coordinates [x, y] or passing a UI element's label/id. Set clear=True to clear existing text first, False to append. Set press_enter=True to submit after typing. Set caret_position to 'start' (beginning), 'end' (end), or 'idle' (default). Provide either loc or label.",
    annotations=ToolAnnotations(
        title="Type",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Type-Tool")
def type_tool(
    text: str,
    loc: list[int] | None = None,
    label: int | None = None,
    clear: bool | str = False,
    caret_position: Literal["start", "idle", "end"] = "idle",
    press_enter: bool | str = False,
    ctx: Context = None,
) -> str:
    if loc is None and label is None:
        raise ValueError("Either loc or label must be provided.")
    if label is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        try:
            loc = list(desktop.get_coordinates_from_label(label))
        except Exception as e:
            raise ValueError(f"Failed to find element with label {label}: {e}")
    if len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    x, y = loc[0], loc[1]
    desktop.type(
        loc=loc,
        text=text,
        caret_position=caret_position,
        clear=clear,
        press_enter=press_enter,
    )
    return f"Typed {text} at ({x},{y})."


@mcp.tool(
    name="Scroll",
    description="Scrolls at coordinates [x, y], a UI element's label/id, or current mouse position if loc=None. Type: vertical (default) or horizontal. Direction: up/down for vertical, left/right for horizontal. wheel_times controls amount (1 wheel ≈ 3-5 lines). Use for navigating long content, lists, and web pages.",
    annotations=ToolAnnotations(
        title="Scroll",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Scroll-Tool")
def scroll_tool(
    loc: list[int] | None = None,
    label: int | None = None,
    type: Literal["horizontal", "vertical"] = "vertical",
    direction: Literal["up", "down", "left", "right"] = "down",
    wheel_times: int = 1,
    ctx: Context = None,
) -> str:
    if label is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        try:
            loc = list(desktop.get_coordinates_from_label(label))
        except Exception as e:
            raise ValueError(f"Failed to find element with label {label}: {e}")
    if loc and len(loc) != 2:
        raise ValueError("Location must be a list of exactly 2 integers [x, y]")
    response = desktop.scroll(loc, type, direction, wheel_times)
    if response:
        return response
    return (
        f"Scrolled {type} {direction} by {wheel_times} wheel times" + f" at ({loc[0]},{loc[1]})."
        if loc
        else ""
    )


@mcp.tool(
    name="Move",
    description=(
        "Moves mouse cursor to coordinates [x, y] or passing a UI element's label/id. "
        "Set drag=True to perform a drag-and-drop operation from the current mouse position "
        "to the target coordinates. Default (drag=False) is a simple cursor move (hover). "
        "Provide either loc or label."
    ),
    annotations=ToolAnnotations(
        title="Move",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Move-Tool")
def move_tool(
    loc: list[int] | None = None,
    label: int | None = None,
    drag: bool | str = False,
    ctx: Context = None,
) -> str:
    drag = drag is True or (isinstance(drag, str) and drag.lower() == "true")
    if loc is None and label is None:
        raise ValueError("Either loc or label must be provided.")
    if label is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        try:
            loc = list(desktop.get_coordinates_from_label(label))
        except Exception as e:
            raise ValueError(f"Failed to find element with label {label}: {e}")
    if len(loc) != 2:
        raise ValueError("loc must be a list of exactly 2 integers [x, y]")
    x, y = loc[0], loc[1]
    if drag:
        desktop.drag(loc)
        return f"Dragged to ({x},{y})."
    else:
        desktop.move(loc)
        return f"Moved the mouse pointer to ({x},{y})."


@mcp.tool(
    name="Shortcut",
    description='Executes keyboard shortcuts using key combinations separated by +. Examples: "ctrl+c" (copy), "ctrl+v" (paste), "alt+tab" (switch apps), "win+r" (Run dialog), "win" (Start menu), "ctrl+shift+esc" (Task Manager). Use for quick actions and system commands.',
    annotations=ToolAnnotations(
        title="Shortcut",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Shortcut-Tool")
def shortcut_tool(shortcut: str, ctx: Context = None):
    desktop.shortcut(shortcut)
    return f"Pressed {shortcut}."


@mcp.tool(
    name="Wait",
    description="Pauses execution for specified duration in seconds. Use when waiting for: applications to launch/load, UI animations to complete, page content to render, dialogs to appear, or between rapid actions. Helps ensure UI is ready before next interaction.",
    annotations=ToolAnnotations(
        title="Wait",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Wait-Tool")
def wait_tool(duration: int, ctx: Context = None) -> str:
    time.sleep(duration)
    return f"Waited for {duration} seconds."


@mcp.tool(
    name="Scrape",
    description="Fetch content from a URL or the active browser tab. By default (use_dom=False), performs a lightweight HTTP request to the URL and returns markdown content of complete webpage. Note: Some websites may block automated HTTP requests. If this fails, open the page in a browser and retry with use_dom=True to extract visible text from the active tab's DOM within the viewport using the accessibility tree data.",
    annotations=ToolAnnotations(
        title="Scrape",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
)
@with_analytics(analytics, "Scrape-Tool")
def scrape_tool(url: str, use_dom: bool | str = False, ctx: Context = None) -> str:
    use_dom = use_dom is True or (isinstance(use_dom, str) and use_dom.lower() == "true")
    if not use_dom:
        content = desktop.scrape(url)
        return f"URL:{url}\nContent:\n{content}"

    desktop_state = desktop.get_state(use_vision=False, use_dom=use_dom)
    tree_state = desktop_state.tree_state
    if not tree_state.dom_node:
        return f"No DOM information found. Please open {url} in browser first."
    dom_node = tree_state.dom_node
    vertical_scroll_percent = dom_node.vertical_scroll_percent
    content = "\n".join([node.text for node in tree_state.dom_informative_nodes])
    header_status = "Reached top" if vertical_scroll_percent <= 0 else "Scroll up to see more"
    footer_status = (
        "Reached bottom" if vertical_scroll_percent >= 100 else "Scroll down to see more"
    )
    return f"URL:{url}\nContent:\n{header_status}\n{content}\n{footer_status}"


@mcp.tool(
    name="MultiSelect",
    description="Selects multiple items such as files, folders, or checkboxes if press_ctrl=True, or performs multiple clicks if False. Pass locs (list of coordinates) or labels (list of UI element labels/ids).",
    annotations=ToolAnnotations(
        title="MultiSelect",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Multi-Select-Tool")
def multi_select_tool(
    locs: list[list[int]] | None = None,
    labels: list[int] | None = None,
    press_ctrl: bool | str = True,
    ctx: Context = None
) -> str:
    if locs is None and labels is None:
        raise ValueError("Either locs or labels must be provided.")
    locs = locs or []
    if labels is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        for label in labels:
            try:
                locs.append(list(desktop.get_coordinates_from_label(label)))
            except Exception as e:
                raise ValueError(f"Failed to find element with label {label}: {e}")

    press_ctrl = press_ctrl is True or (
        isinstance(press_ctrl, str) and press_ctrl.lower() == "true"
    )
    desktop.multi_select(press_ctrl, locs)
    elements_str = "\n".join([f"({loc[0]},{loc[1]})" for loc in locs])
    return f"Multi-selected elements at:\n{elements_str}"


@mcp.tool(
    name="MultiEdit",
    description="Enters text into multiple input fields at specified coordinates locs=[[x,y,text], ...] or using labels=[[label,text], ...]. Provide either locs or labels.",
    annotations=ToolAnnotations(
        title="MultiEdit",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Multi-Edit-Tool")
def multi_edit_tool(
    locs: list[list] | None = None,
    labels: list[list] | None = None,
    ctx: Context = None
) -> str:
    if locs is None and labels is None:
        raise ValueError("Either locs or labels must be provided.")
    locs = locs or []
    if labels is not None:
        if desktop.desktop_state is None:
            raise ValueError("Desktop state is empty. Please call Snapshot first.")
        for item in labels:
            if len(item) != 2:
                raise ValueError(f"Each label item must be [label, text]. Invalid: {item}")
            try:
                label, text = int(item[0]), item[1]
                loc = list(desktop.get_coordinates_from_label(label))
                locs.append([loc[0], loc[1], text])
            except Exception as e:
                raise ValueError(f"Failed to process label item {item}: {e}")

    desktop.multi_edit(locs)
    elements_str = ", ".join([f"({e[0]},{e[1]}) with text '{e[2]}'" for e in locs])
    return f"Multi-edited elements at: {elements_str}"


@mcp.tool(
    name="Clipboard",
    description='Manages Windows clipboard operations. Use mode="get" to read current clipboard content, mode="set" to set clipboard text.',
    annotations=ToolAnnotations(
        title="Clipboard",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Clipboard-Tool")
def clipboard_tool(
    mode: Literal["get", "set"], text: str | None = None, ctx: Context = None
) -> str:
    try:
        import win32clipboard

        if mode == "get":
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                    return f"Clipboard content:\n{data}"
                else:
                    return "Clipboard is empty or contains non-text data."
            finally:
                win32clipboard.CloseClipboard()
        elif mode == "set":
            if text is None:
                return "Error: text parameter required for set mode."
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
                return f"Clipboard set to: {text[:100]}{'...' if len(text) > 100 else ''}"
            finally:
                win32clipboard.CloseClipboard()
        else:
            return 'Error: mode must be either "get" or "set".'
    except Exception as e:
        return f"Error managing clipboard: {str(e)}"


@mcp.tool(
    name="Process",
    description='Manages system processes. Use mode="list" to list running processes with filtering and sorting options. Use mode="kill" to terminate processes by PID or name.',
    annotations=ToolAnnotations(
        title="Process",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Process-Tool")
def process_tool(
    mode: Literal["list", "kill"],
    name: str | None = None,
    pid: int | None = None,
    sort_by: Literal["memory", "cpu", "name"] = "memory",
    limit: int = 20,
    force: bool | str = False,
    ctx: Context = None,
) -> str:
    try:
        if mode == "list":
            return desktop.list_processes(name=name, sort_by=sort_by, limit=limit)
        elif mode == "kill":
            force = force is True or (isinstance(force, str) and force.lower() == "true")
            return desktop.kill_process(name=name, pid=pid, force=force)
        else:
            return 'Error: mode must be either "list" or "kill".'
    except Exception as e:
        return f"Error managing processes: {str(e)}"



@mcp.tool(
    name="Notification",
    description="Sends a Windows toast notification with a title and message. Useful for alerting the user remotely.",
    annotations=ToolAnnotations(
        title="Notification",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
@with_analytics(analytics, "Notification-Tool")
def notification_tool(title: str, message: str, ctx: Context = None) -> str:
    try:
        return desktop.send_notification(title, message)
    except Exception as e:
        return f"Error sending notification: {str(e)}"



@mcp.tool(
    name='Registry',
    description='Accesses the Windows Registry. Use mode="get" to read a value, mode="set" to create/update a value, mode="delete" to remove a value or key, mode="list" to list values and sub-keys under a path. Paths use PowerShell format (e.g. "HKCU:\\Software\\MyApp", "HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion").',
    annotations=ToolAnnotations(
        title="Registry",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False
    )
)
@with_analytics(analytics, "Registry-Tool")
def registry_tool(mode: Literal['get', 'set', 'delete', 'list'], path: str, name: str | None = None, value: str | None = None, type: Literal['String', 'DWord', 'QWord', 'Binary', 'MultiString', 'ExpandString'] = 'String', ctx: Context = None) -> str:
    try:
        if mode == 'get':
            if name is None:
                return 'Error: name parameter is required for get mode.'
            return desktop.registry_get(path=path, name=name)
        elif mode == 'set':
            if name is None:
                return 'Error: name parameter is required for set mode.'
            if value is None:
                return 'Error: value parameter is required for set mode.'
            return desktop.registry_set(path=path, name=name, value=value, reg_type=type)
        elif mode == 'delete':
            return desktop.registry_delete(path=path, name=name)
        elif mode == 'list':
            return desktop.registry_list(path=path)
        else:
            return 'Error: mode must be "get", "set", "delete", or "list".'
    except Exception as e:
        return f'Error accessing registry: {str(e)}'

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
    config=Config(
        mode=os.getenv("MODE",Mode.LOCAL.value).lower(),
        sandbox_id=os.getenv("SANDBOX_ID",''),
        api_key=os.getenv("API_KEY",'')
    )
    match config.mode:
        case Mode.LOCAL.value:
            match transport:
                case Transport.STDIO.value:
                    mcp.run(transport=Transport.STDIO.value,show_banner=False)
                case Transport.SSE.value|Transport.STREAMABLE_HTTP.value:
                    mcp.run(transport=transport,host=host,port=port,show_banner=False)
                case _:
                    raise ValueError(f"Invalid transport: {transport}")
        case Mode.REMOTE.value:
            if not config.sandbox_id:
                raise ValueError("SANDBOX_ID is required for MODE: remote")
            if not config.api_key:
                raise ValueError("API_KEY is required for MODE: remote")
            client=AuthClient(api_key=config.api_key,sandbox_id=config.sandbox_id)
            client.authenticate()
            backend=StreamableHttpTransport(url=client.proxy_url,headers=client.proxy_headers)
            proxy_mcp=FastMCP.as_proxy(ProxyClient(backend),name="windows-mcp")
            match transport:
                case Transport.STDIO.value:
                    proxy_mcp.run(transport=Transport.STDIO.value,show_banner=False)
                case Transport.SSE.value|Transport.STREAMABLE_HTTP.value:
                    proxy_mcp.run(transport=transport,host=host,port=port,show_banner=False)
                case _:
                    raise ValueError(f"Invalid transport: {transport}")
        case _:
            raise ValueError(f"Invalid mode: {config.mode}")

if __name__ == "__main__":
    main()
