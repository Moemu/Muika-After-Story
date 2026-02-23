from __future__ import annotations

from ..registry import register_tool
from ..tools import FetchWebContentTool
from .rss import extract_web_content


@register_tool("fetch_web_content")
async def handle_fetch_web_content(tool: FetchWebContentTool) -> str:
    return await extract_web_content(tool.url)
