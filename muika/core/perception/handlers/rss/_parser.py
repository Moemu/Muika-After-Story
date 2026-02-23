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
    """
    解析 RSS 内容，返回解析结果列表

    :param rss_content: RSS 内容的字节串或字符串
    :return: 解析结果列表
    """
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
    """
    异步获取 RSS/网页 内容的字节串

    :param link: RSS/网页链接
    :return: RSS/网页内容的字节串
    """
    async with ClientSession() as session:
        async with session.get(link) as response:
            return await response.read()


async def extract_web_content(url: str) -> str:
    """
    通过文章链接提取网页正文内容

    :param url: 文章链接
    :return: 提取的正文内容字符串
    """
    html = await fetch_web_content(url)
    content = trafilatura.extract(html)
    return content if content else ""
