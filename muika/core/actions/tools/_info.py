from __future__ import annotations

from pydantic import BaseModel, Field

from muika.plugin.func_call import on_function_call
from muika.plugin.func_call._context import get_state
from muika.utils.logger import logger

from .rss import AVAILABLE_RSS_SOURCES


class CheckRSSUpdateParams(BaseModel):
    rss_source: str = Field(
        ...,
        description=f"RSS source identifier. Available sources: {AVAILABLE_RSS_SOURCES}",
    )


@on_function_call(
    "Fetch and summarize updates from a configured RSS source.",
    params=CheckRSSUpdateParams,
)
async def check_rss_update(rss_source: str):
    from .rss import RSS_SOURCES, fetch_web_content, parse_rss_feed

    rss = RSS_SOURCES.get(rss_source)
    if not rss:
        return f"Unknown RSS source: {rss_source!r}"

    logger.debug(f"[CheckRSSUpdate] Fetching: {rss.url}")
    feed_data = await fetch_web_content(rss.url)
    entries = parse_rss_feed(feed_data)

    lines = [f"# RSS Feed Update from {rss.name}:"]
    for entry in entries:
        lines.append(
            f"- title: {entry.title}; description: {entry.description};"
            f" link: {entry.link}; published: {entry.published}"
        )

    state = get_state()
    if state:
        state.boredom *= 0.3
        state.curiosity = min(1.0, state.curiosity + 0.2)
        state.attention = min(1.0, state.attention + 0.1)

    return "\n".join(lines)


class FetchWebContentParams(BaseModel):
    url: str = Field(..., description="The URL of the web content to fetch. Must be a valid http/https URL.")


@on_function_call(
    "Extract plain text content from a web page.",
    params=FetchWebContentParams,
)
async def fetch_web_content(url: str):
    from urllib.parse import urlparse

    from .rss import extract_web_content

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Invalid URL {url!r}: only http/https is supported. "
            f"If no suitable tool exists for this task, report that directly."
        )

    logger.debug(f"[FetchWebContent] Fetching: {url}")
    content = await extract_web_content(url)
    return content


class SearchWikipediaParams(BaseModel):
    query: str = Field(..., description="Search term or article title to look up on Wikipedia.")
    language: str = Field(
        "zh",
        description="Wikipedia language code, e.g. 'zh' for Chinese, 'en' for English, 'ja' for Japanese.",
    )


@on_function_call(
    "Search Wikipedia and return a summary of the best matching article.",
    params=SearchWikipediaParams,
)
async def search_wikipedia(query: str, language: str = "zh"):
    from urllib.parse import quote

    from aiohttp import ClientSession

    lang = language.strip().lower() or "zh"
    q = query.strip()

    # Step 1: OpenSearch to resolve best-matching page title
    search_url = (
        f"https://{lang}.wikipedia.org/w/api.php"
        f"?action=opensearch&search={quote(q)}&limit=1&namespace=0&format=json"
    )
    logger.debug(f"[SearchWikipedia] OpenSearch: {search_url}")
    try:
        async with ClientSession() as session:
            async with session.get(search_url, timeout=__import__("aiohttp").ClientTimeout(total=10)) as resp:
                result = await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"[SearchWikipedia] Search failed: {e}")
        return f"Wikipedia search failed: {e}"

    titles: list[str] = result[1] if len(result) > 1 else []
    if not titles:
        return f"No Wikipedia article found for: {q!r}"

    page_title = titles[0]

    # Step 2: Fetch page summary via REST API
    summary_url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{quote(page_title)}"
    logger.debug(f"[SearchWikipedia] Summary: {summary_url}")
    try:
        async with ClientSession() as session:
            async with session.get(summary_url, timeout=__import__("aiohttp").ClientTimeout(total=10)) as resp:
                data = await resp.json(content_type=None)
    except Exception as e:
        logger.error(f"[SearchWikipedia] Summary fetch failed: {e}")
        return f"Failed to fetch Wikipedia summary: {e}"

    title = data.get("title", page_title)
    extract = data.get("extract", "")
    page_url = data.get("content_urls", {}).get("desktop", {}).get("page", summary_url)

    if not extract:
        return f"Wikipedia article '{title}' has no summary available."

    state = get_state()
    if state:
        state.curiosity = min(1.0, state.curiosity + 0.15)

    return f"# {title}\n\n{extract}\n\nSource: {page_url}"
