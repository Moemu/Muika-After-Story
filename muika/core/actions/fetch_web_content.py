from __future__ import annotations

from ..intents import FetchWebContentIntent
from ._registry import register_action
from .rss import extract_web_content


@register_action("fetch_web_content")
async def handle_fetch_web_content(intent: FetchWebContentIntent) -> str:
    return await extract_web_content(intent.url)
