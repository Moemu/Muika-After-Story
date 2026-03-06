"""Entrypoint for built-in immediate tools.

Tool implementations are split by capability into dedicated modules:
- _base.py:        base class contract (BaseTool.is_enabled() method)
- _info.py:        web and RSS information retrieval
- _device.py:      screenshot and camera capture
- _system.py:      system observation and notifications
- _memory.py:      long-term memory operations
- _filesystem.py:  file system read/write (enabled via BaseTool.is_enabled())
- _executor.py:    Python subprocess execution (enabled via BaseTool.is_enabled())

All classes are always imported; availability is controlled at runtime via
the BaseTool.is_enabled() method on each tool class. Butler's _leaf_action_classes
filters out tools where is_enabled() returns False.
"""

from ._base import BaseTool
from ._device import CaptureCameraPhotoTool, CaptureScreenshotTool
from ._executor import ExecutePythonTool
from ._filesystem import DeleteFileTool, ListDirectoryTool, ReadFileTool, WriteFileTool
from ._info import CheckRSSUpdateTool, FetchWebContentTool, SearchWikipediaTool
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
    "SearchWikipediaTool",
    "CaptureScreenshotTool",
    "CaptureCameraPhotoTool",
    "ListProcessesTool",
    "GetFocusedWindowTool",
    "GetSystemStatusTool",
    "SendDesktopNotificationTool",
    "ReadClipboardTool",
    "MemoryTool",
    "ListDirectoryTool",
    "ReadFileTool",
    "WriteFileTool",
    "DeleteFileTool",
    "ExecutePythonTool",
]
