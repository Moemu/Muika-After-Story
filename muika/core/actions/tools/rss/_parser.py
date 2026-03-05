from dataclasses import dataclass

import feedparser
import trafilatura
from aiohttp import ClientSession


@dataclass
class ParsedResult:
    title: str
    link: str
    published: str
    description: str


def parse_rss_feed(rss_content: bytes | str) -> list[ParsedResult]:
    feed = feedparser.parse(rss_content)
    items = []
    for entry in feed.entries:
        item = ParsedResult(
            title=entry.get("title", ""),  # type: ignore
            link=entry.get("link", ""),  # type: ignore
            published=entry.get("published", ""),  # type: ignore
            description=entry.get("description", ""),  # type: ignore
        )
        items.append(item)
    return items


async def fetch_web_content(link: str) -> bytes:
    async with ClientSession() as session:
        async with session.get(link) as response:
            return await response.read()


async def extract_web_content(url: str) -> str:
    html = await fetch_web_content(url)
    content = trafilatura.extract(html)
    return content if content else ""
