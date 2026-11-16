"""内置联网搜索插件与搜索后端。"""

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from aiohttp import ClientSession, ClientTimeout
from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.state import MuikaState
from muika.llm.utils.tools import ToolError
from muika.plugin import PluginMetadata
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

from .config import WebSearchConfig, plugin_config

metadata = PluginMetadata(
    name="web_search",
    description="联网搜索，支持 Tavily、Perplexity 和 DeepSeek",
    usage="设置 WEB_SEARCH_PROVIDER 和 WEB_SEARCH_API_KEY 后，由 Muika 调用 web_search。",
    config=WebSearchConfig,
)

_TAVILY_ENDPOINT = "https://api.tavily.com/search"
_PERPLEXITY_ENDPOINT = "https://api.perplexity.ai/search"
_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/tools/search"

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


def _parse_results(data: object, provider: str, snippet_fields: tuple[str, ...], date_field: str) -> list[SearchResult]:
    """归一化搜索响应，跳过缺少 URL 的异常条目。"""
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise ValueError(f"unexpected {provider} response shape: 'results' list missing")

    results = []
    for item in data["results"]:
        if not isinstance(item, dict) or not item.get("url"):
            continue
        text = next((item[field] for field in snippet_fields if item.get(field)), "")
        snippet = " ".join(str(text).split())[:_SNIPPET_LIMIT]
        results.append(
            SearchResult(
                title=str(item.get("title") or item["url"]),
                url=str(item["url"]),
                snippet=snippet,
                date=str(item[date_field]) if item.get(date_field) else None,
            )
        )
    return results


def _parse_tavily_response(data: object) -> list[SearchResult]:
    return _parse_results(data, "Tavily", ("content",), "published_date")


def _parse_perplexity_response(data: object) -> list[SearchResult]:
    return _parse_results(data, "Perplexity", ("snippet",), "date")


def _parse_deepseek_response(data: object) -> list[SearchResult]:
    return _parse_results(data, "DeepSeek", ("summary", "snippet"), "date")


async def tavily_search(query: str, time_range: str | None) -> list[SearchResult]:
    """调用 Tavily Search API 返回排序结果。"""
    payload: dict[str, object] = {"query": query, "max_results": _MAX_RESULTS}
    if time_range:
        payload["time_range"] = time_range

    async with ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(
            _TAVILY_ENDPOINT,
            json=payload,
            headers={"Authorization": f"Bearer {plugin_config.api_key}"},
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
            headers={"Authorization": f"Bearer {plugin_config.api_key}"},
            proxy=mas_config.proxy,
        ) as resp:
            resp.raise_for_status()
            return _parse_perplexity_response(await resp.json(content_type=None))


async def deepseek_search(query: str, time_range: str | None) -> list[SearchResult]:
    """调用 DeepSeek Search API，用日期搜索语法过滤时效。"""
    if time_range:
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[time_range]
        after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
        query = f"{query} after:{after}"
    if len(query) > 128:
        raise ValueError("DeepSeek search query must not exceed 128 characters, including any date filter.")

    async with ClientSession(timeout=_REQUEST_TIMEOUT) as session:
        async with session.post(
            _DEEPSEEK_ENDPOINT,
            json={"query": query, "count": _MAX_RESULTS},
            headers={"Authorization": f"Bearer {plugin_config.api_key}"},
            proxy=mas_config.proxy,
        ) as resp:
            resp.raise_for_status()
            return _parse_deepseek_response(await resp.json(content_type=None))


SEARCH_BACKENDS: dict[str, SearchBackend] = {
    "tavily": tavily_search,
    "perplexity": perplexity_search,
    "deepseek": deepseek_search,
}
"""可用的搜索后端，键为 ``WEB_SEARCH_PROVIDER`` 配置值。"""


def _format_results(query: str, results: list[SearchResult]) -> str:
    """把归一化结果渲染为模型可读的纯文本列表。"""
    lines = [f'# Web search results for "{query}":', ""]
    for index, result in enumerate(results[:_MAX_RESULTS], start=1):
        lines.append(f"{index}. {result.title}")
        lines.append(f"   {result.url}")
        if result.snippet:
            lines.append(f"   {result.snippet}")
        if result.date:
            lines.append(f"   published: {result.date}")
    return "\n".join(lines)


@on_function_call(
    "Search the web for current information. Returns ranked results with titles, URLs and snippets;"
    " follow a promising result with fetch_web_content to read the full page. Cite the sources you actually"
    " used, and if search is unconfigured or returns nothing useful, say so plainly instead of inventing facts.",
    params=WebSearchParams,
    read_only=True,
)
async def web_search(query: str, state: MuikaState, time_range: str | None = None) -> str:
    """联网搜索并返回结果列表；未配置后端时提示不可用。"""
    q = query.strip()
    if not q:
        return ToolError("Search query is empty.")

    provider = (plugin_config.provider or "").strip().lower()
    if not provider or not plugin_config.api_key:
        return ToolError(
            "Web search is not configured. Ask the player to set WEB_SEARCH_PROVIDER "
            "('tavily', 'perplexity' or 'deepseek') and WEB_SEARCH_API_KEY. Report that web search is unavailable."
        )
    if provider not in SEARCH_BACKENDS:
        return ToolError(
            f"Unknown web search provider: {provider!r}. " f"Available providers: {', '.join(sorted(SEARCH_BACKENDS))}."
        )
    if time_range is not None and time_range not in _ALLOWED_TIME_RANGES:
        return ToolError(f"Invalid time_range {time_range!r}: allowed values are 'day', 'week', 'month' and 'year'.")

    logger.debug(f"[WebSearch] provider={provider} query={q!r} time_range={time_range!r}")
    try:
        results = await SEARCH_BACKENDS[provider](q, time_range)
    except Exception as e:
        logger.error(f"[WebSearch] Search failed: {e}")
        return ToolError(f"Web search failed: {e}")

    if not results:
        return f'No search results for: "{q}"'

    state.curiosity = min(1.0, state.curiosity + 0.15)
    return _format_results(q, results)
