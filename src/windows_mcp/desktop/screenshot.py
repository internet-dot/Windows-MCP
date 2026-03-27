import logging
import os
from typing import Callable

from PIL import Image, ImageGrab

try:
    import dxcam
except Exception:
    dxcam = None

try:
    import mss
except ImportError:
    mss = None


logger = logging.getLogger(__name__)


def get_screenshot_backend() -> str:
    value = os.getenv("WINDOWS_MCP_SCREENSHOT_BACKEND", "auto")
    normalized = value.strip().lower()
    if normalized in {"auto", "pillow", "dxcam", "mss"}:
        return normalized
    logger.warning(
        "Unknown screenshot backend '%s'; falling back to auto",
        value,
    )
    return "auto"


def resolve_dxcam_region(
    capture_rect,
    get_monitors_rect: Callable[[], list],
) -> tuple[int, tuple[int, int, int, int] | None] | None:
    if capture_rect is None:
        return 0, None

    monitor_rects = get_monitors_rect()
    for output_idx, monitor_rect in enumerate(monitor_rects):
        if (
            monitor_rect.left <= capture_rect.left
            and monitor_rect.top <= capture_rect.top
            and monitor_rect.right >= capture_rect.right
            and monitor_rect.bottom >= capture_rect.bottom
        ):
            if monitor_rect == capture_rect:
                return output_idx, None
            return output_idx, (
                capture_rect.left - monitor_rect.left,
                capture_rect.top - monitor_rect.top,
                capture_rect.right - monitor_rect.left,
                capture_rect.bottom - monitor_rect.top,
            )
    return None


def get_dxcam_camera(output_idx: int, camera_cache: dict[int, object], dxcam_module=None):
    module = dxcam_module if dxcam_module is not None else dxcam
    if module is None:
        raise RuntimeError("dxcam is not available")

    camera = camera_cache.get(output_idx)
    if camera is None:
        camera = module.create(output_idx=output_idx, processor_backend="numpy")
        camera_cache[output_idx] = camera
    return camera


def capture_with_dxcam(
    capture_rect,
    get_monitors_rect: Callable[[], list],
    camera_cache: dict[int, object],
    dxcam_module=None,
) -> Image.Image:
    resolved = resolve_dxcam_region(capture_rect, get_monitors_rect)
    if resolved is None:
        raise ValueError("DXGI capture supports only regions fully contained within one display")

    output_idx, region = resolved
    camera = get_dxcam_camera(output_idx, camera_cache, dxcam_module=dxcam_module)
    frame = camera.grab(region=region, copy=True, new_frame_only=False)
    if frame is None:
        raise RuntimeError("DXGI capture returned no frame")
    return Image.fromarray(frame)


def capture_with_pillow(capture_rect, crop_screenshot: Callable[[Image.Image, object], Image.Image]) -> Image.Image:
    grab_kwargs = {"all_screens": True}
    if capture_rect is not None:
        grab_kwargs["bbox"] = (
            capture_rect.left,
            capture_rect.top,
            capture_rect.right,
            capture_rect.bottom,
        )
    try:
        screenshot = ImageGrab.grab(**grab_kwargs)
    except (OSError, RuntimeError, ValueError):
        if capture_rect is not None:
            logger.warning(
                "Failed to capture selected region directly, falling back to virtual screen crop"
            )
            return crop_screenshot(ImageGrab.grab(all_screens=True), capture_rect)
        logger.warning("Failed to capture virtual screen, using primary screen")
        screenshot = ImageGrab.grab()
    return crop_screenshot(screenshot, capture_rect)


def capture_with_mss(capture_rect, crop_screenshot: Callable[[Image.Image, object], Image.Image], mss_module=None) -> Image.Image:
    module = mss_module if mss_module is not None else mss
    if module is None:
        raise RuntimeError("mss is not available")

    with module.mss() as sct:
        if capture_rect is None:
            monitor = sct.monitors[0]
        else:
            monitor = {
                "left": capture_rect.left,
                "top": capture_rect.top,
                "width": capture_rect.right - capture_rect.left,
                "height": capture_rect.bottom - capture_rect.top,
            }
        raw = sct.grab(monitor)
        image = Image.frombytes("RGB", raw.size, raw.rgb)
    return crop_screenshot(image, capture_rect)


def _auto_backend_chain() -> list[str]:
    return ["dxcam", "mss", "pillow"]


def capture(
    capture_rect,
    crop_screenshot: Callable[[Image.Image, object], Image.Image],
    get_monitors_rect: Callable[[], list],
    camera_cache: dict[int, object],
    backend: str | None = None,
    dxcam_module=None,
    mss_module=None,
) -> tuple[Image.Image, str]:
    selected = backend or get_screenshot_backend()
    chain = _auto_backend_chain() if selected == "auto" else [selected]

    for backend_name in chain:
        try:
            if backend_name == "dxcam":
                if capture_rect is None:
                    continue
                if (dxcam_module if dxcam_module is not None else dxcam) is None:
                    continue
                return (
                    capture_with_dxcam(
                        capture_rect,
                        get_monitors_rect,
                        camera_cache,
                        dxcam_module=dxcam_module,
                    ),
                    "dxcam",
                )

            if backend_name == "mss":
                if (mss_module if mss_module is not None else mss) is None:
                    continue
                return (
                    capture_with_mss(capture_rect, crop_screenshot, mss_module=mss_module),
                    "mss",
                )

            if backend_name == "pillow":
                return (capture_with_pillow(capture_rect, crop_screenshot), "pillow")

        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "Screenshot backend '%s' failed; trying next backend",
                backend_name,
                exc_info=selected != "auto",
            )

    # Final safety fallback so capture always returns an image on supported hosts.
    return (capture_with_pillow(capture_rect, crop_screenshot), "pillow")
