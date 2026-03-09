from unittest.mock import patch

import pytest
from PIL import Image

from windows_mcp.desktop.service import Desktop
from windows_mcp.desktop.views import DesktopState, Size, Status, Window
from windows_mcp.tree.views import BoundingBox, Center, ScrollElementNode, TreeElementNode, TreeState
from windows_mcp.uia import Rect


def make_box(left: int, top: int, right: int, bottom: int) -> BoundingBox:
    return BoundingBox(
        left=left,
        top=top,
        right=right,
        bottom=bottom,
        width=right - left,
        height=bottom - top,
    )


class TestParseDisplaySelection:
    def test_none_keeps_default_behavior(self):
        assert Desktop.parse_display_selection(None) is None
        assert Desktop.parse_display_selection("") is None

    def test_supports_single_and_multiple_displays(self):
        assert Desktop.parse_display_selection([0]) == [0]
        assert Desktop.parse_display_selection([0, 1]) == [0, 1]
        assert Desktop.parse_display_selection([0, 1, 0]) == [0, 1]
        assert Desktop.parse_display_selection(2) == [2]

    def test_rejects_invalid_display_values(self):
        with pytest.raises(ValueError):
            Desktop.parse_display_selection("0")
        with pytest.raises(ValueError):
            Desktop.parse_display_selection([-1])
        with pytest.raises(ValueError):
            Desktop.parse_display_selection(["a"])


class TestDisplayFiltering:
    @pytest.fixture
    def desktop(self):
        with patch.object(Desktop, "__init__", lambda self: None):
            return Desktop()

    def test_get_display_union_rect(self, desktop):
        with patch("windows_mcp.desktop.service.uia.GetMonitorsRect") as mock_get_monitors:
            mock_get_monitors.return_value = [
                Rect(0, 0, 1920, 1080),
                Rect(1920, 0, 3840, 1080),
            ]

            result = desktop.get_display_union_rect([1])
            assert result == Rect(1920, 0, 3840, 1080)

            combined = desktop.get_display_union_rect([0, 1])
            assert combined == Rect(0, 0, 3840, 1080)

    def test_get_display_union_rect_rejects_missing_display(self, desktop):
        with patch("windows_mcp.desktop.service.uia.GetMonitorsRect") as mock_get_monitors:
            mock_get_monitors.return_value = [Rect(0, 0, 1920, 1080)]
            with pytest.raises(ValueError):
                desktop.get_display_union_rect([1])

    def test_filter_state_to_selected_display(self, desktop):
        region = make_box(1920, 0, 3840, 1080)
        kept_window = Window(
            name="Browser",
            is_browser=True,
            depth=0,
            status=Status.NORMAL,
            bounding_box=make_box(1800, 100, 2200, 500),
            handle=1,
            process_id=11,
        )
        dropped_window = Window(
            name="Editor",
            is_browser=False,
            depth=0,
            status=Status.NORMAL,
            bounding_box=make_box(100, 100, 600, 600),
            handle=2,
            process_id=22,
        )
        tree_state = TreeState(
            interactive_nodes=[
                TreeElementNode(
                    name="Visible",
                    control_type="Button",
                    window_name="Browser",
                    bounding_box=make_box(2000, 200, 2100, 260),
                    center=Center(x=2050, y=230),
                    metadata={},
                ),
                TreeElementNode(
                    name="Hidden",
                    control_type="Button",
                    window_name="Editor",
                    bounding_box=make_box(200, 200, 260, 260),
                    center=Center(x=230, y=230),
                    metadata={},
                ),
            ],
            scrollable_nodes=[
                ScrollElementNode(
                    name="Pane",
                    control_type="Pane",
                    window_name="Browser",
                    bounding_box=make_box(1900, 0, 2500, 900),
                    center=Center(x=2200, y=450),
                    metadata={"vertical_scrollable": True},
                )
            ],
        )

        filtered_tree = desktop._filter_tree_state_to_region(tree_state, region)
        filtered_window = desktop._filter_window_to_region(kept_window, region)
        filtered_windows = desktop._filter_windows_to_region([kept_window, dropped_window], region)

        assert filtered_window is not None
        assert filtered_window.bounding_box.left == 1920
        assert filtered_window.bounding_box.right == 2200
        assert [window.name for window in filtered_windows] == ["Browser"]
        assert [node.name for node in filtered_tree.interactive_nodes] == ["Visible"]
        assert filtered_tree.scrollable_nodes[0].bounding_box.left == 1920
        assert filtered_tree.root_node.bounding_box == region

    def test_crop_screenshot_to_display_region(self, desktop):
        screenshot = Image.new("RGB", (3840, 1080), "white")
        with patch("windows_mcp.desktop.service.uia.GetVirtualScreenRect") as mock_virtual_rect:
            mock_virtual_rect.return_value = (0, 0, 3840, 1080)
            cropped = desktop._crop_screenshot(
                screenshot,
                Rect(1920, 0, 3840, 1080),
            )
        assert cropped.size == (1920, 1080)

    def test_grid_lines_use_selected_display_region(self, desktop):
        screenshot = Image.new("RGB", (3840, 1080), "white")
        with patch.object(desktop, "get_screenshot", return_value=screenshot):
            with patch("windows_mcp.desktop.service.uia.GetVirtualScreenRect") as mock_virtual_rect:
                mock_virtual_rect.return_value = (0, 0, 3840, 1080)
                annotated = desktop.get_annotated_screenshot(
                    nodes=[],
                    grid_lines=(2, 2),
                    capture_rect=Rect(1920, 0, 3840, 1080),
                )

        assert annotated.size == (1920, 1080)
        assert annotated.getpixel((960, 100)) != (255, 255, 255)
        assert annotated.getpixel((100, 540)) != (255, 255, 255)

    def test_desktop_state_tracks_selected_displays(self):
        state = DesktopState(
            active_desktop={"name": "Desktop 1"},
            all_desktops=[{"name": "Desktop 1"}],
            active_window=None,
            windows=[],
            screenshot_size=Size(width=1920, height=1080),
            screenshot_region=make_box(1920, 0, 3840, 1080),
            screenshot_displays=[1],
        )
        assert state.screenshot_size.to_string() == "(1920,1080)"
        assert state.screenshot_region.xyxy_to_string() == "(1920,0,3840,1080)"
        assert state.screenshot_displays == [1]
