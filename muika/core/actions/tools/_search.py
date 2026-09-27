"""联网搜索工具与可插拔后端。"""

from typing import Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout
from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.state import MuikaState
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/search"

_MAX_RESULTS = 5
"""返回给模型的搜索结果条数上限。"""
_SNIPPET_LIMIT = 800
"""单条结果摘要的字符上限，控制工具输出体积。"""
_REQUEST_TIMEOUT = ClientTimeout(total=15)

_ALLOWED_TIME_RANGES = ("day", "week", "month", "year")


class WebSearchParams(BaseModel):
    query: str = Field(..., description="Search query. Keep it concise, in the language of the topic.")
    time_range: str | None = Field(
        None,
        description=(
            "Optional freshness filter: 'day', 'week', 'month' or 'year'. "
            "Honored when the configured search backend supports it."
        ),
    )


class SearchResult(BaseModel):
    """归一化后的单条搜索结果，与具体后端的响应字段解耦。"""

    title: str
    url: str
    snippet: str = ""
    date: str | None = None


SearchBackend = Callable[[str, str | None], Awaitable[list[SearchResult]]]


def _parse_tavily_response(data: object) -> list[SearchResult]:
    """解析 Tavily Search API 响应，跳过缺少 URL 的异常条目。"""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("unexpected Tavily response shape: 'results' list missing")

    results = []
    for item in data["results"]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        snippet = " ".join(str(item.get("content") or "").split())[:_SNIPPET_LIMIT]
        results.append(
            SearchResult(
                title=str(item.get("title") or item["url"]),
                url=str(item["url"]),
                snippet=snippet,
                date=str(item["published_date"]) if item.get("published_date") else None,
            )
        )
    return results


def _parse_perplexity_response(data: object) -> list[SearchResult]:
    """解析 Perplexity Search API 响应，跳过缺少 URL 的异常条目。"""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError("unexpected Perplexity response shape: 'results' list missing")

    results = []
    for item in data["results"]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        snippet = " ".join(str(item.get("snippet") or "").split())[:_SNIPPET_LIMIT]
        results.append(
            SearchResult(
                title=str(item.get("title") or item["url"]),
                url=str(item["url"]),
                snippet=snippet,
                date=str(item["date"]) if item.get("date") else None,
            )
        )
    return results


async def tavily_search(query: str, time_range: str | None) -> list[SearchResult]:
    """调用 Tavily Search API 返回排序结果。"""
    payload: dict[str, object] = {"query": query, "max_results": _MAX_RESULTS}
    if time_range:
        payload["time_range"] = time_range

    async with ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(
            _TAVILY_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {mas_config.web_search_api_key}"},
            proxy=mas_config.proxy,
        ) as resp:
            resp.raise_for_status()
            return _parse_tavily_response(await resp.json(content_type=None))


async def perplexity_search(query: str, time_range: str | None) -> list[SearchResult]:
    """调用 Perplexity Search API 返回排序结果（该后端不支持时效过滤）。"""
    payload: dict[str, object] = {"query": query, "max_results": _MAX_RESULTS}

    async with ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(
            _PERPLEXITY_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {mas_config.web_search_api_key}"},
            proxy=mas_config.proxy,
        ) as resp:
            resp.raise_for_status()
            return _parse_perplexity_response(await resp.json(content_type=None))


SEARCH_BACKENDS: dict[str, SearchBackend] = {
    "tavily": tavily_search,
    "perplexity": perplexity_search,
}
"""可用的搜索后端，键为 ``WEB_SEARCH_PROVIDER`` 配置值。"""


def _format_results(query: str, results: list[SearchResult]) -> str:
    """把归一化结果渲染为模型可读的纯文本列表。"""
    lines = [f'# Web search results for "{query}":', ""]
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. {result.title}")
        lines.append(f"   {result.url}")
        if result.snippet:
            lines.append(f"   {result.snippet}")
        if result.date:
            lines.append(f"   published: {result.date}")
    return "\n".join(lines)


@on_function_call(
    "Search the web for current information. Returns ranked results with titles, URLs and snippets;"
    " use fetch_web_content to read a full page.",
    params=WebSearchParams,
    read_only=True,
)
async def web_search(query: str, state: MuikaState, time_range: str | None = None) -> str:
    """联网搜索并返回结果列表；未配置后端时提示不可用。"""
    q = query.strip()
    if not q:
        return "Search query is empty."

    provider = (mas_config.web_search_provider or "").strip().lower()
    if not provider or not mas_config.web_search_api_key:
        return (
            "Web search is not configured. Ask the player to set WEB_SEARCH_PROVIDER "
            "(e.g. 'tavily' or 'perplexity') and WEB_SEARCH_API_KEY. Report that web search is unavailable."
        )
    if provider not in SEARCH_BACKENDS:
        return (
            f"Unknown web search provider: {provider!r}. " f"Available providers: {', '.join(sorted(SEARCH_BACKENDS))}."
        )
    if time_range is not None and time_range not in _ALLOWED_TIME_RANGES:
        return f"Invalid time_range {time_range!r}: allowed values are 'day', 'week', 'month' and 'year'."

    logger.debug(f"[WebSearch] provider={provider} query={q!r} time_range={time_range!r}")
    try:
        results = await SEARCH_BACKENDS[provider](q, time_range)
    except Exception as e:
        logger.error(f"[WebSearch] Search failed: {e}")
        return f"Web search failed: {e}"

    if not results:
        return f'No search results for: "{q}"'

    state.curiosity = min(1.0, state.curiosity + 0.15)
    return _format_results(q, results)
