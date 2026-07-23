from __future__ import annotations

from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pydantic import Field

from muika.utils.logger import logger

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


class SearchWikipediaTool(BaseTool):
    """Search Wikipedia and return a summary of the best matching article."""

    name: Literal["search_wikipedia"] = "search_wikipedia"
    query: str = Field(
        ...,
        description="Search term or article title to look up on Wikipedia.",
    )
    language: str = Field(
        "zh",
        description="Wikipedia language code, e.g. 'zh' for Chinese, 'en' for English, 'ja' for Japanese.",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        from urllib.parse import quote

        from aiohttp import ClientSession

        lang = self.language.strip().lower() or "zh"
        query = self.query.strip()

        # Step 1: OpenSearch to resolve best-matching page title
        search_url = (
            f"https://{lang}.wikipedia.org/w/api.php"
            f"?action=opensearch&search={quote(query)}&limit=1&namespace=0&format=json"
        )
        logger.debug(f"[SearchWikipediaTool] OpenSearch: {search_url}")
        try:
            async with ClientSession() as session:
                async with session.get(search_url, timeout=__import__("aiohttp").ClientTimeout(total=10)) as resp:
                    result = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[SearchWikipediaTool] Search failed: {e}")
            return ActionOutput(content=f"Wikipedia search failed: {e}")

        titles: list[str] = result[1] if len(result) > 1 else []
        if not titles:
            return ActionOutput(content=f"No Wikipedia article found for: {query!r}")

        page_title = titles[0]

        # Step 2: Fetch page summary via REST API
        summary_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(page_title)}"
        logger.debug(f"[SearchWikipediaTool] Summary: {summary_url}")
        try:
            async with ClientSession() as session:
                async with session.get(summary_url, timeout=__import__("aiohttp").ClientTimeout(total=10)) as resp:
                    data = await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"[SearchWikipediaTool] Summary fetch failed: {e}")
            return ActionOutput(content=f"Failed to fetch Wikipedia summary: {e}")

        title = data.get("title", page_title)
        extract = data.get("extract", "")
        page_url = data.get("content_urls", {}).get("desktop", {}).get("page", summary_url)

        if not extract:
            return ActionOutput(content=f"Wikipedia article '{title}' has no summary available.")

        state.curiosity = min(1.0, state.curiosity + 0.15)
        return ActionOutput(content=f"# {title}\n\n{extract}\n\nSource: {page_url}")
