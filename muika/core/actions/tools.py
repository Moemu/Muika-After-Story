"""
Action Tools — immediate actions that observe environment or fetch external data.

To add a new tool (including from an external plugin):
  1. Subclass BaseTool
  2. Declare Pydantic fields
  3. Implement async def handle(self, state: MuikaState, executor: Executor) -> ActionOutput
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from nonebot import logger
from pydantic import Field

from muika.core.memory import MemoryCategory, MemoryLayer

from .rss import AVAILABLE_RSS_SOURCES
from .schema import ActionMode, ActionOutput, BaseAction

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class BaseTool(BaseAction):
    """Base class for immediate actions."""

    mode: Literal[ActionMode.IMMEDIATE] = ActionMode.IMMEDIATE

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        raise NotImplementedError(f"{type(self).__name__} must implement handle()")


class CheckRSSUpdateTool(BaseTool):
    name: Literal["check_rss_update"] = "check_rss_update"
    rss_source: str = Field(
        ...,
        description=f"RSS source identifier. Available sources: {AVAILABLE_RSS_SOURCES}",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        from .rss import RSS_SOURCES, fetch_web_content, parse_rss_feed

        rss_source = RSS_SOURCES.get(self.rss_source)
        if not rss_source:
            logger.warning(f"[CheckRSSUpdateTool] Unknown RSS source: {self.rss_source!r}")
            raise ValueError(f"Unknown RSS source: {self.rss_source!r}")

        logger.debug(f"[CheckRSSUpdateTool] Fetching: {rss_source.url}")
        feed_data = await fetch_web_content(rss_source.url)
        entries = parse_rss_feed(feed_data)

        lines = [f"# RSS Feed Update from {rss_source.name}:"]
        for entry in entries:
            lines.append(
                f"- title: {entry.title}; description: {entry.description};"
                f" link: {entry.link}; published: {entry.published}"
            )

        state.boredom *= 0.3
        state.curiosity = min(1.0, state.curiosity + 0.2)
        state.attention = min(1.0, state.attention + 0.1)
        return ActionOutput(content="\n".join(lines))


class FetchWebContentTool(BaseTool):
    name: Literal["fetch_web_content"] = "fetch_web_content"
    url: str = Field(..., description="The URL of the web content to fetch. Must be a valid http/https URL.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        from urllib.parse import urlparse

        from .rss import extract_web_content

        parsed = urlparse(self.url)
        if parsed.scheme not in ("http", "https"):
            return ActionOutput(
                content=f"[FetchWebContentTool] Invalid URL {self.url!r}: only http/https is supported. "
                f"If no suitable tool exists for this task, report that directly."
            )
        logger.debug(f"[FetchWebContentTool] Fetching: {self.url}")
        content = await extract_web_content(self.url)
        return ActionOutput(content=content)


class CaptureScreenshotTool(BaseTool):
    name: Literal["capture_screenshot"] = "capture_screenshot"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        from datetime import datetime
        from pathlib import Path
        from tempfile import gettempdir

        from PIL import ImageGrab

        from muika.models import Resource

        temp_dir = Path(gettempdir()) / "muika_screenshots"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            screenshot = ImageGrab.grab()
            screenshot.thumbnail((1920, 1080))
            timestamp = int(datetime.now().timestamp())
            file_path = temp_dir / f"screenshot_{timestamp}.png"
            screenshot.save(file_path)
            logger.info(f"[CaptureScreenshotTool] Saved to {file_path}")
            resource = Resource(type="image", path=str(file_path), mimetype="image/png")
            return ActionOutput(
                content=f"Screenshot captured successfully. Path: {file_path}",
                resources=[resource],
            )
        except Exception as e:
            logger.error(f"[CaptureScreenshotTool] Failed: {e}")
            return ActionOutput(content=f"Failed to capture screenshot: {e}")


class ListProcessesTool(BaseTool):
    name: Literal["list_processes"] = "list_processes"
    filter: Optional[str] = Field(None, description="Optional filter string for process names.")
    limit: int = Field(50, description="Maximum number of processes to return.")
    offset: int = Field(0, description="Number of processes to skip for pagination.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        import psutil

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
    name: Literal["get_focused_window"] = "get_focused_window"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
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


# ---------------------------------------------------------------------------
# Memory Tool
# ---------------------------------------------------------------------------


class MemoryTool(BaseTool):
    """Read, write, or forget a fact in Muika's long-term memory."""

    name: Literal["memory"] = "memory"
    type: Literal["remember", "forget", "read"] = Field(
        ...,
        description=(
            "'remember': store a key-value fact; "
            "'forget': delete a stored key; "
            "'read': list stored memories (optionally filtered by category)."
        ),
    )
    category: MemoryCategory = Field(
        MemoryCategory.USER,
        description=(
            "Memory category: 'user' for user facts, 'self' for self-knowledge, "
            "'world' for world facts, 'relation' for relationship state."
        ),
    )
    layer: MemoryLayer = Field(
        MemoryLayer.PREFERENCE,
        description=(
            "Which memory layer to write to. Choose carefully:\n"
            "'core'= CoreIdentity. Use for stable, high-confidence facts that define who the user IS "
            "or critical relationship anchors. Always injected into every system prompt. "
            "Examples: user's preferred name/nickname, confirmed occupation, confirmed daily schedule, "
            "first conversation date, a firmly stated long-term preference.\n"
            "'state'= RelationshipState. Use for recent, time-sensitive context that matters "
            "only for the current resumption of conversation. Expires naturally. "
            "Examples: topic of last conversation, recent emotional tone, an unresolved question, "
            "a recent disagreement.\n"
            "'preference' = PreferenceProfile. Use for long-term soft preferences and lifestyle facts "
            "that are useful but NOT identity-defining. Retrieved on demand, not always injected. "
            "Examples: favourite music genre, preferred coffee type, hobbies, sleep habits.\n"
            "'archive'= ArchiveMemory. Reserved for session summaries — do NOT use directly.\n"
            "RULE: If in doubt between 'core' and 'preference', ask: "
            "'Would forgetting this change how I should address or understand this person fundamentally?' "
            "If yes → 'core'. If no → 'preference'."
        ),
    )
    key: Optional[str] = Field(
        None,
        description="Memory key, required for 'remember' and 'forget'.",
    )
    value: Optional[str] = Field(
        None,
        description="Memory value, required for 'remember'.",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        if state.memory is None:
            return ActionOutput(content="[MemoryTool] MemoryManager not available.")

        if self.type == "read":
            mem = state.memory.records
            if not mem:
                return ActionOutput(content="No memories stored yet.")
            lines = [
                f"[{v.layer.value}/{v.category.value}] {v.key}: {v.value}"
                for _, v in sorted(mem.items(), key=lambda x: x[1].layer.value)
                if self.category is None or v.category == self.category
            ]
            return ActionOutput(content="\n".join(lines) if lines else "No matching memories found.")

        if self.type == "remember":
            if not self.key or self.value is None:
                return ActionOutput(content="[MemoryTool] 'key' and 'value' are required for 'remember'.")
            await state.memory.upsert_memory(
                layer=self.layer,
                category=self.category,
                key=self.key,
                value=self.value,
            )
            logger.info(f"[MemoryTool] Saved [{self.layer.value}/{self.category.value}] {self.key} = {self.value!r}")
            return ActionOutput(
                content=f"Memory saved — [{self.layer.value}/{self.category.value}] {self.key} = {self.value!r}",
                silent=True,
            )

        # type == "forget"
        if not self.key:
            return ActionOutput(content="[MemoryTool] 'key' is required for 'forget'.")
        await state.memory.forget_memory(layer=self.layer, category=self.category, key=self.key)
        logger.info(f"[MemoryTool] Forgot [{self.layer.value}/{self.category.value}] {self.key}")
        return ActionOutput(
            content=f"Memory forgotten — [{self.layer.value}/{self.category.value}] {self.key}",
            silent=True,
        )
