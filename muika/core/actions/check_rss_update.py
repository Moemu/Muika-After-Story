from __future__ import annotations

from nonebot import logger

from ..intents import CheckRSSUpdateIntent
from ..state import MuikaState
from ._registry import register_action
from .rss import RSS_SOURCES, fetch_web_content, parse_rss_feed


@register_action("check_rss_update")
async def handle_check_rss_update(intent: CheckRSSUpdateIntent, state: MuikaState) -> str:
    rss_source = RSS_SOURCES.get(intent.rss_source)
    if not rss_source:
        logger.warning(f"Unknown RSS source: {intent.rss_source}")
        raise ValueError(f"Unknown RSS source: {intent.rss_source}")

    logger.debug(f"Checking RSS feed: {rss_source.url}")
    feed_data = await fetch_web_content(rss_source.url)
    feed_contents = parse_rss_feed(feed_data)
    logger.debug(f"Fetched {len(feed_contents)} entries from RSS feed.")

    feed_outlines = [f"# RSS Feed Update from {rss_source.name}: \n"]
    for entry in feed_contents:
        outline = (
            f"- title: {entry.title}; description: {entry.description};"
            f" link: {entry.link}; published: {entry.published}\n"
        )
        feed_outlines.append(outline)

    # 行为反作用
    state.boredom *= 0.3
    state.curiosity = min(1.0, state.curiosity + 0.2)
    state.attention = min(1.0, state.attention + 0.1)
    return "\n".join(feed_outlines)
