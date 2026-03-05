"""Compatibility entrypoint for built-in immediate tools.

Tool implementations are split by capability into dedicated modules:
- _base.py: base class contract
- _info.py: web and RSS information retrieval
- _device.py: screenshot and camera capture
- _system.py: system observation and notifications
- _memory.py: long-term memory operations
"""

from ._base import BaseTool
from ._device import CaptureCameraPhotoTool, CaptureScreenshotTool
from ._info import CheckRSSUpdateTool, FetchWebContentTool
from ._memory import MemoryTool
from ._system import (
    GetFocusedWindowTool,
    GetSystemStatusTool,
    ListProcessesTool,
    ReadClipboardTool,
    SendDesktopNotificationTool,
)

__all__ = [
    "BaseTool",
    "CheckRSSUpdateTool",
    "FetchWebContentTool",
    "CaptureScreenshotTool",
    "CaptureCameraPhotoTool",
    "ListProcessesTool",
    "GetFocusedWindowTool",
    "GetSystemStatusTool",
    "SendDesktopNotificationTool",
    "ReadClipboardTool",
    "MemoryTool",
]
