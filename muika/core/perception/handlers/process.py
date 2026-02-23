from __future__ import annotations

import psutil
import win32gui
import win32process
from nonebot import logger

from ..registry import register_tool
from ..tools import GetFocusedWindowTool, GetSystemStatusTool, ListProcessesTool


@register_tool("get_focused_window")
async def handle_get_focused_window(tool: GetFocusedWindowTool) -> str:
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "No active window detected."

        window_title = win32gui.GetWindowText(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)

        try:
            process = psutil.Process(pid)
            process_name = process.name()
        except psutil.NoSuchProcess:
            process_name = "Unknown"

        return f"Focused Window: '{window_title}' (Process: {process_name}, PID: {pid})"
    except Exception as e:
        logger.error(f"Failed to get focused window: {e}")
        return f"Error getting focused window: {str(e)}"


@register_tool("list_processes")
async def handle_list_processes(tool: ListProcessesTool) -> str:
    try:
        processes = set()
        ignored = {
            "svchost.exe",
            "System",
            "Registry",
            "smss.exe",
            "csrss.exe",
            "wininit.exe",
            "services.exe",
            "lsass.exe",
        }

        for proc in psutil.process_iter(["name"]):
            try:
                p_name = proc.info["name"]
                if p_name in ignored:
                    continue
                if tool.filter:
                    if tool.filter.lower() in p_name.lower():
                        processes.add(p_name)
                else:
                    processes.add(p_name)

                if len(processes) >= 500:  # Hard limit to prevent excessive iteration
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        unique_names = sorted(list(processes))
        count = len(unique_names)

        start = tool.offset
        end = start + tool.limit
        display_names = unique_names[start:end]

        output = f"Running Processes (Total unique: {count}):\n"
        output += ", ".join(display_names)

        if count > end:
            output += f"\n...and {count - end} more. Use offset={end} to see more."

        return output
    except Exception as e:
        logger.error(f"Failed to list processes: {e}")
        return f"Error listing processes: {str(e)}"


@register_tool("get_system_status")
async def handle_get_system_status(tool: GetSystemStatusTool) -> str:
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        dataset = psutil.virtual_memory()
        memory_percent = dataset.percent

        status = f"System Status:\nCPU Usage: {cpu_percent}%\nMemory Usage: {memory_percent}%"

        battery = psutil.sensors_battery()
        if battery:
            plugged = "Plugged In" if battery.power_plugged else "On Battery"
            status += f"\nBattery: {battery.percent}% ({plugged})"

        return status
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        return f"Error getting system status: {str(e)}"
