from .perception import handle_capture_screenshot
from .process import (
    handle_get_focused_window,
    handle_get_system_status,
    handle_list_processes,
)

__all__ = [
    "handle_capture_screenshot",
    "handle_list_processes",
    "handle_get_focused_window",
    "handle_get_system_status",
]
