from dataclasses import dataclass

from aiohttp import ClientSession


@dataclass
class ParsedResult:
    title: str
    link: str
    published: str
    description: str


def parse_rss_feed(rss_content: bytes | str) -> list[ParsedResult]:
    """
    解析 RSS feed 内容，提取每条新闻的标题、链接、发布时间和描述信息。

    :param rss_content: RSS feed 的原始内容，可以是字节串或字符串。
    :return: 包含解析结果的列表，每个元素是一个 ParsedResult 对象
    """
    import feedparser

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
    直接从链接获取网页内容的原始字节数据。
    """
    async with ClientSession() as session:
        async with session.get(link) as response:
            return await response.read()


async def extract_web_content(url: str) -> str:
    """
    从指定 URL 获取网页内容，并使用 trafilatura 提取纯文本内容。
    """
    import trafilatura

    html = await fetch_web_content(url)
    content = trafilatura.extract(html)
    return content if content else ""
