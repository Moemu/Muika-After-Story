from __future__ import annotations

import psutil
import win32gui
import win32process
from nonebot import logger

from ...intents import (
    GetFocusedWindowIntent,
    GetSystemStatusIntent,
    ListProcessesIntent,
)
from .._registry import register_action


@register_action("get_focused_window")
async def handle_get_focused_window(intent: GetFocusedWindowIntent) -> str:
    """
    获取当前活动窗口的标题和进程名
    """
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


@register_action("list_processes")
async def handle_list_processes(intent: ListProcessesIntent) -> str:
    """
    列出当前运行的进程。
    为了避免 token 爆炸，只列出前 50 个唯一的进程名，排除系统关键进程。
    """
    try:
        processes = []
        for proc in psutil.process_iter(["name"]):
            try:
                p_name = proc.info["name"]
                if intent.filter:
                    if intent.filter.lower() in p_name.lower():
                        processes.append(p_name)
                else:
                    processes.append(p_name)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass

        unique_names = sorted(list(set(processes)))

        # 过滤掉一些常见的系统后台进程以减少噪声 (可选)
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
        filtered_names = [n for n in unique_names if n not in ignored]

        count = len(filtered_names)
        display_names = filtered_names[:50]  # 最多显示50个

        output = f"Running Processes (Total unique: {count}):\n"
        output += ", ".join(display_names)

        if count > 50:
            output += f"\n...and {count - 50} more."

        return output
    except Exception as e:
        logger.error(f"Failed to list processes: {e}")
        return f"Error listing processes: {str(e)}"


@register_action("get_system_status")
async def handle_get_system_status(intent: GetSystemStatusIntent) -> str:
    """
    获取系统状态 (CPU, 内存, 电池)
    """
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
