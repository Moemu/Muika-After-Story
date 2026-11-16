"""联网搜索插件的独立配置与供应商 HTTP 协议。"""

from datetime import datetime, timezone

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from muika.core.state import MuikaState
from muika.llm.utils.tools import ToolError


def test_search_config_defaults_and_dotenv(web_search_plugin, monkeypatch, tmp_path):
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    monkeypatch.delenv("WEB_SEARCH_API_KEY", raising=False)
    config = web_search_plugin.WebSearchConfig(_env_file=None)
    assert config.provider is None
    assert config.api_key == ""
    dotenv = tmp_path / ".env"
    dotenv.write_text("MASTER_ID=test\nWEB_SEARCH_PROVIDER=deepseek\nWEB_SEARCH_API_KEY=dotenv-key\n", encoding="utf-8")
    config = web_search_plugin.WebSearchConfig(_env_file=dotenv)
    assert config.provider == "deepseek"
    assert config.api_key == "dotenv-key"
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "")
    assert web_search_plugin.WebSearchConfig(_env_file=dotenv).provider is None
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("WEB_SEARCH_API_KEY", "environment-key")
    config = web_search_plugin.WebSearchConfig(_env_file=dotenv)
    assert config.provider == "tavily"
    assert config.api_key == "environment-key"


@pytest.mark.parametrize(
    ("provider", "endpoint_name", "time_range", "payload", "result_fields"),
    [
        (
            "tavily",
            "_TAVILY_ENDPOINT",
            "week",
            {"query": "release news", "max_results": 5, "time_range": "week"},
            {"content": "  Search   summary \n ", "published_date": "2026-11-12"},
        ),
        (
            "perplexity",
            "_PERPLEXITY_ENDPOINT",
            "week",
            {"query": "release news", "max_results": 5},
            {"snippet": "Search summary", "date": "2026-11-12"},
        ),
        (
            "deepseek",
            "_DEEPSEEK_ENDPOINT",
            "week",
            {"query": "release news after:2026-11-05", "count": 5},
            {"summary": "Search summary", "snippet": "Short summary", "content": "", "date": "2026-11-12"},
        ),
        (
            "deepseek",
            "_DEEPSEEK_ENDPOINT",
            None,
            {"query": "release news", "count": 5},
            {"snippet": "Search summary", "date": "2026-11-12"},
        ),
        (
            "deepseek",
            "_DEEPSEEK_ENDPOINT",
            "day",
            {"query": "release news after:2026-11-11", "count": 5},
            {"summary": "Search summary", "date": "2026-11-12"},
        ),
        (
            "deepseek",
            "_DEEPSEEK_ENDPOINT",
            "month",
            {"query": "release news after:2026-10-13", "count": 5},
            {"summary": "Search summary", "date": "2026-11-12"},
        ),
        (
            "deepseek",
            "_DEEPSEEK_ENDPOINT",
            "year",
            {"query": "release news after:2025-11-12", "count": 5},
            {"summary": "Search summary", "date": "2026-11-12"},
        ),
    ],
)
async def test_search_provider_http_contract(
    web_search_plugin, monkeypatch, provider, endpoint_name, time_range, payload, result_fields
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 12, 12, tzinfo=timezone.utc)

    requests = []

    async def search(request):
        requests.append((request.path, request.headers["Authorization"], await request.json()))
        return web.json_response(
            {"results": [{"title": "Release", "url": "https://example.com/news", **result_fields}]}
        )

    application = web.Application()
    application.router.add_post("/search", search)
    async with TestServer(application) as server:
        monkeypatch.setattr(web_search_plugin, endpoint_name, str(server.make_url("/search")))
        monkeypatch.setattr(web_search_plugin, "datetime", FixedDatetime)
        monkeypatch.setattr(web_search_plugin.mas_config, "proxy", None)
        monkeypatch.setattr(web_search_plugin.plugin_config, "provider", provider)
        monkeypatch.setattr(web_search_plugin.plugin_config, "api_key", "search-key")
        state = MuikaState(curiosity=0.9)
        result = await web_search_plugin.web_search(" release news ", state, time_range)

    assert not isinstance(result, ToolError), result
    assert requests == [("/search", "Bearer search-key", payload)]
    assert "Release" in result
    assert "https://example.com/news" in result
    assert "Search summary" in result
    assert "published: 2026-11-12" in result
    assert state.curiosity == 1.0


@pytest.mark.parametrize(
    ("query", "time_range", "valid"),
    [
        ("界" * 128, None, True),
        ("界" * 129, None, False),
        ("x" * 111, "week", True),
        ("x" * 112, "week", False),
    ],
)
async def test_deepseek_query_length_before_request(web_search_plugin, monkeypatch, query, time_range, valid):
    requests = []

    async def search(request):
        requests.append(await request.json())
        return web.json_response({"results": []})

    application = web.Application()
    application.router.add_post("/search", search)
    async with TestServer(application) as server:
        monkeypatch.setattr(web_search_plugin, "_DEEPSEEK_ENDPOINT", str(server.make_url("/search")))
        monkeypatch.setattr(web_search_plugin.mas_config, "proxy", None)
        monkeypatch.setattr(web_search_plugin.plugin_config, "provider", "deepseek")
        monkeypatch.setattr(web_search_plugin.plugin_config, "api_key", "search-key")
        result = await web_search_plugin.web_search(query, MuikaState(), time_range)

    assert len(requests) == int(valid)
    assert isinstance(result, ToolError) is not valid
    if valid:
        assert len(requests[0]["query"]) == 128
    else:
        assert "128 characters" in result


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (401, {"error": {"message": "Invalid API key"}}, "401"),
        (429, {"error": {"message": "Rate limit reached"}}, "429"),
        (500, {"error": {"message": "Internal server error"}}, "500"),
        (200, {"results": None}, "'results' list missing"),
    ],
)
async def test_deepseek_http_errors_are_tool_errors(web_search_plugin, monkeypatch, status, body, error):
    async def search(request):
        return web.json_response(body, status=status)

    application = web.Application()
    application.router.add_post("/search", search)
    async with TestServer(application) as server:
        monkeypatch.setattr(web_search_plugin, "_DEEPSEEK_ENDPOINT", str(server.make_url("/search")))
        monkeypatch.setattr(web_search_plugin.mas_config, "proxy", None)
        monkeypatch.setattr(web_search_plugin.plugin_config, "provider", "deepseek")
        monkeypatch.setattr(web_search_plugin.plugin_config, "api_key", "search-key")
        state = MuikaState(curiosity=0.5)
        result = await web_search_plugin.web_search("news", state)

    assert isinstance(result, ToolError)
    assert error in result
    assert state.curiosity == 0.5
