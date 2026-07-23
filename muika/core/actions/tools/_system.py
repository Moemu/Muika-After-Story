from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from pydantic import Field

from muika.utils.logger import logger

from ..schema import ActionOutput
from ._base import BaseTool

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class ListProcessesTool(BaseTool):
    """List running processes with optional keyword filtering."""

    name: Literal["list_processes"] = "list_processes"
    filter: Optional[str] = Field(None, description="Optional filter string for process names.")
    limit: int = Field(50, description="Maximum number of processes to return.")
    offset: int = Field(0, description="Number of processes to skip for pagination.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        import sys

        import psutil

        ignored = (
            {
                "svchost.exe",
                "System",
                "Registry",
                "smss.exe",
                "csrss.exe",
                "wininit.exe",
                "services.exe",
                "lsass.exe",
            }
            if sys.platform == "win32"
            else {"kthreadd", "ksoftirqd", "kworker"}
        )
        try:
            processes: set[str] = set()
            for proc in psutil.process_iter(["name"]):
                try:
                    name = proc.info["name"]
                    if name in ignored:
                        continue
                    if self.filter and self.filter.lower() not in name.lower():
                        continue
                    processes.add(name)
                    if len(processes) >= 500:
                        break
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            sorted_names = sorted(processes)
            total = len(sorted_names)
            page = sorted_names[self.offset : self.offset + self.limit]
            out = f"Running Processes (Total unique: {total}):\n" + ", ".join(page)
            if total > self.offset + self.limit:
                remaining = total - self.offset - self.limit
                next_offset = self.offset + self.limit
                out += f"\n...and {remaining} more. Use offset={next_offset} to see more."
            return ActionOutput(content=out)
        except Exception as e:
            logger.error(f"[ListProcessesTool] Failed: {e}")
            return ActionOutput(content=f"Error listing processes: {e}")


class GetFocusedWindowTool(BaseTool):
    """Get the title and process of the currently focused window."""

    name: Literal["get_focused_window"] = "get_focused_window"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        import sys

        if sys.platform != "win32":
            return ActionOutput(content="[GetFocusedWindowTool] Not supported on this platform (Windows only).")

        import psutil
        import win32gui
        import win32process

        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return ActionOutput(content="No active window detected.")
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                process_name = psutil.Process(pid).name()
            except psutil.NoSuchProcess:
                process_name = "Unknown"
            return ActionOutput(content=f"Focused Window: '{title}' (Process: {process_name}, PID: {pid})")
        except Exception as e:
            logger.error(f"[GetFocusedWindowTool] Failed: {e}")
            return ActionOutput(content=f"Error getting focused window: {e}")


class GetSystemStatusTool(BaseTool):
    """Get system load indicators such as CPU, memory, and battery."""

    name: Literal["get_system_status"] = "get_system_status"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        import psutil

        try:
            cpu = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory().percent
            status = f"System Status:\nCPU Usage: {cpu}%\nMemory Usage: {mem}%"
            battery = psutil.sensors_battery()
            if battery:
                plugged = "Plugged In" if battery.power_plugged else "On Battery"
                status += f"\nBattery: {battery.percent:.0f}% ({plugged})"
            return ActionOutput(content=status)
        except Exception as e:
            logger.error(f"[GetSystemStatusTool] Failed: {e}")
            return ActionOutput(content=f"Error getting system status: {e}")


class SendDesktopNotificationTool(BaseTool):
    """Send a desktop notification to the user."""

    name: Literal["send_desktop_notification"] = "send_desktop_notification"
    title: str = Field(..., description="Title of the desktop notification.")
    message: str = Field(..., description="Body text of the desktop notification.")
    timeout: int = Field(5, description="How many seconds the notification stays visible (default 5).")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            from plyer import notification
        except ImportError:
            return ActionOutput(
                content="[SendDesktopNotificationTool] plyer is not installed. " "Run: pip install plyer"
            )

        import asyncio

        def _notify() -> None:
            notification.notify(
                title=self.title,
                message=self.message,
                app_name="Muika",
                timeout=self.timeout,
            )  # type: ignore

        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, _notify)
            logger.info(f"[SendDesktopNotificationTool] Sent: {self.title!r}")
            return ActionOutput(content=f"Desktop notification sent: {self.title!r}")
        except Exception as e:
            logger.error(f"[SendDesktopNotificationTool] Failed: {e}")
            return ActionOutput(content=f"Failed to send desktop notification: {e}")


class ReadClipboardTool(BaseTool):
    """Read the current text content of the system clipboard."""

    name: Literal["read_clipboard"] = "read_clipboard"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            import pyperclip
        except ImportError:
            return ActionOutput(content="[ReadClipboardTool] pyperclip is not installed. " "Run: pip install pyperclip")

        import asyncio

        loop = asyncio.get_event_loop()
        try:
            text = await loop.run_in_executor(None, pyperclip.paste)
            if not text:
                return ActionOutput(content="Clipboard is empty.")
            max_len = 2000
            truncated = text[:max_len]
            suffix = f"\n...(truncated, {len(text) - max_len} chars omitted)" if len(text) > max_len else ""
            logger.debug(f"[ReadClipboardTool] Read {len(text)} chars from clipboard")
            return ActionOutput(content=f"Clipboard content:\n{truncated}{suffix}")
        except pyperclip.PyperclipException as e:
            return ActionOutput(
                content=(
                    f"[ReadClipboardTool] Clipboard unavailable (Linux requires xclip or xsel: apt install xclip)"
                    f": {e}"
                )
            )
        except Exception as e:
            logger.error(f"[ReadClipboardTool] Failed: {e}")
            return ActionOutput(content=f"Failed to read clipboard: {e}")
