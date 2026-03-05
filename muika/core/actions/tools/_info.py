from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from nonebot import logger
from pydantic import Field

from ..schema import ActionOutput
from ._base import BaseTool
from .rss import AVAILABLE_RSS_SOURCES

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class CheckRSSUpdateTool(BaseTool):
    """Fetch and summarize updates from a configured RSS source."""

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
    """Extract plain text content from a web page."""

    name: Literal["fetch_web_content"] = "fetch_web_content"
    url: str = Field(..., description="The URL of the web content to fetch. Must be a valid http/https URL.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
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
