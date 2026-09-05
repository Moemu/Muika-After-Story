from __future__ import annotations

import asyncio
import sys
from typing import Optional

import psutil
from pydantic import BaseModel, Field

from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

if sys.platform == "win32":
    try:
        import win32gui
        import win32process
    except ImportError:
        win32gui = None
        win32process = None
else:
    win32gui = None
    win32process = None

try:
    from plyer import notification
except ImportError:
    notification = None

try:
    import pyperclip
except ImportError:
    pyperclip = None


class ListProcessesParams(BaseModel):
    filter: Optional[str] = Field(None, description="Optional filter string for process names.")
    limit: int = Field(50, description="Maximum number of processes to return.")
    offset: int = Field(0, description="Number of processes to skip for pagination.")


@on_function_call(
    "List running processes with optional keyword filtering.",
    params=ListProcessesParams,
)
async def list_processes(filter: Optional[str] = None, limit: int = 50, offset: int = 0) -> str:
    """列出匹配的进程名称，并按偏移量分页。"""

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
                if filter and filter.lower() not in name.lower():
                    continue
                processes.add(name)
                if len(processes) >= 500:
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        sorted_names = sorted(processes)
        total = len(sorted_names)
        page = sorted_names[offset : offset + limit]
        out = f"Running Processes (Total unique: {total}):\n" + ", ".join(page)
        if total > offset + limit:
            remaining = total - offset - limit
            next_offset = offset + limit
            out += f"\n...and {remaining} more. Use offset={next_offset} to see more."
        return out
    except Exception as e:
        logger.error(f"[ListProcesses] Failed: {e}")
        return f"Error listing processes: {e}"


@on_function_call("Get the title and process of the currently focused window.")
async def get_focused_window() -> str:
    """读取当前窗口信息，依赖不可用时返回明确提示。"""
    if sys.platform != "win32":
        return "Not supported on this platform (Windows only)."
    if win32gui is None or win32process is None:
        return "Focused window information is unavailable: pywin32 could not be loaded. Install or repair pywin32."

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "No active window detected."
        title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            process_name = psutil.Process(pid).name()
        except psutil.NoSuchProcess:
            process_name = "Unknown"
        return f"Focused Window: '{title}' (Process: {process_name}, PID: {pid})"
    except Exception as e:
        logger.error(f"[GetFocusedWindow] Failed: {e}")
        return f"Error getting focused window: {e}"


@on_function_call("Get system load indicators such as CPU, memory, and battery.")
async def get_system_status():

    try:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        status = f"System Status:\nCPU Usage: {cpu}%\nMemory Usage: {mem}%"
        battery = psutil.sensors_battery()
        if battery:
            plugged = "Plugged In" if battery.power_plugged else "On Battery"
            status += f"\nBattery: {battery.percent:.0f}% ({plugged})"
        return status
    except Exception as e:
        logger.error(f"[GetSystemStatus] Failed: {e}")
        return f"Error getting system status: {e}"


class SendDesktopNotificationParams(BaseModel):
    title: str = Field(..., description="Title of the desktop notification.")
    message: str = Field(..., description="Body text of the desktop notification.")
    timeout: int = Field(5, description="How many seconds the notification stays visible (default 5).")


@on_function_call(
    "Send a desktop notification to the user.",
    params=SendDesktopNotificationParams,
)
async def send_desktop_notification(title: str, message: str, timeout: int = 5) -> str:
    """发送桌面通知并返回执行结果。"""
    if notification is None:
        return "plyer is not installed. Run: pip install plyer"

    def _notify() -> None:
        notification.notify(
            title=title,
            message=message,
            app_name="Muika",
            timeout=timeout,
        )  # type: ignore

    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, _notify)
        logger.info(f"[SendDesktopNotification] Sent: {title!r}")
        return f"Desktop notification sent: {title!r}"
    except Exception as e:
        logger.error(f"[SendDesktopNotification] Failed: {e}")
        return f"Failed to send desktop notification: {e}"


@on_function_call("Read the current text content of the system clipboard.")
async def read_clipboard():
    if pyperclip is None:
        return "pyperclip is not installed. Run: pip install pyperclip"

    loop = asyncio.get_event_loop()
    try:
        text = await loop.run_in_executor(None, pyperclip.paste)
        if not text:
            return "Clipboard is empty."
        max_len = 2000
        truncated = text[:max_len]
        suffix = f"\n...(truncated, {len(text) - max_len} chars omitted)" if len(text) > max_len else ""
        logger.debug(f"[ReadClipboard] Read {len(text)} chars from clipboard")
        return f"Clipboard content:\n{truncated}{suffix}"
    except pyperclip.PyperclipException as e:
        return f"Clipboard unavailable (Linux requires xclip or xsel: apt install xclip)" f": {e}"
    except Exception as e:
        logger.error(f"[ReadClipboard] Failed: {e}")
        return f"Failed to read clipboard: {e}"
