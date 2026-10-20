"""后台阅读摘要（DigestAgent）：真实 RSS/正文抓取与真实 SQLite 下的评估边界。

从 ``tests/test_digest_agent.py`` 迁移到真实边界 ``DigestAgent.fetch_and_digest()``：
超长正文在评估 prompt 中被截断、过短条目跳过评估模型、单源评估失败不阻断整轮摘要。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from muika.core.actions.tools.rss._schema import RSSSource
from tests.e2e.harness import ScriptedTurn

pytestmark = pytest.mark.e2e

KEEP_ASSESSMENT = (
    '{"score":85,"keep":true,"reason":"Shared curiosity","primary_theme":"culture",'
    '"summary":"A reading note about how books connect people."}'
)
TRUNCATION_MARKER = "...(truncated)"
LONG_TAIL = "TAIL_AFTER_LIMIT"


def _rss_handler(
    title: str,
    article_path: str,
    hits: list[str] | None = None,
) -> Callable[[web.Request], Awaitable[web.Response]]:
    """RSS feed；条目链接解析为同一 TestServer 上的绝对地址。"""

    async def handler(request: web.Request) -> web.Response:
        if hits is not None:
            hits.append(request.path)
        link = str(request.url.with_path(article_path).with_query(None))
        return web.Response(
            text=(
                '<rss version="2.0"><channel><title>Reading</title><item>'
                f"<title>{title}</title><link>{link}</link>"
                "<description>Reading together</description></item></channel></rss>"
            ),
            content_type="application/rss+xml",
        )

    return handler


def _article_handler(paragraph: str, hits: list[str] | None = None) -> Callable[[web.Request], Awaitable[web.Response]]:
    """文章页；正文由真实 trafilatura 提取。"""

    async def handler(request: web.Request) -> web.Response:
        if hits is not None:
            hits.append(request.path)
        return web.Response(
            text=f"<html><body><article><p>{paragraph}</p></article></body></html>",
            content_type="text/html",
        )

    return handler


def _patch_sources(monkeypatch, client: TestClient, paths: dict[str, str]) -> None:
    """把 RSS 源替换为本用例 TestServer 的地址。"""
    monkeypatch.setattr(
        "muika.core.digest_agent.RSS_SOURCES",
        {
            source_id: RSSSource(
                id=source_id,
                name=source_id.title(),
                url=str(client.make_url(path)),
                language="en",
                topics=["culture"],
                digest=True,
            )
            for source_id, path in paths.items()
        },
    )


@pytest.fixture(autouse=True)
def keep_digest_model(monkeypatch):
    """剧本模型已是场景唯一模型，无需按配置重建 provider 实例。"""
    monkeypatch.setattr("muika.core.digest_agent.refresh_model", lambda model, config: model)


async def test_long_article_is_truncated_in_assessment_prompt(core_app_factory, monkeypatch):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                text=KEEP_ASSESSMENT,
                when=lambda request: request.purpose == "digest_topic_fit",
                name="long_reading",
            )
        ]
    )
    await app.start()
    assert app.muika is not None

    application = web.Application()
    application.router.add_get("/rss", _rss_handler("Long reading", "/article"))
    # 正文超过 DigestAgent._MAX_CONTENT_CHARS（3000），尾部哨兵必须被截掉。
    application.router.add_get("/article", _article_handler("word " * 800 + LONG_TAIL))
    async with TestClient(TestServer(application)) as client:
        _patch_sources(monkeypatch, client, {"reading": "/rss"})
        await asyncio.wait_for(app.muika.digest_agent.fetch_and_digest(), 10)

    call = next(item for item in app.scripted.calls if item["name"] == "long_reading")
    assert TRUNCATION_MARKER in call["prompt"]
    assert LONG_TAIL not in call["prompt"]
    assert call["format"] == "json"

    queued = list(app.muika.topic_manager._event_queue)
    assert len(queued) == 1
    assert queued[0].id.startswith("event_rss_reading_")
    assert queued[0].title == "Long reading"
    assert queued[0].content

    cached = app.db_query("SELECT source_id, keep, score FROM rss_digest_cache")
    assert cached == [{"source_id": "reading", "keep": 1, "score": 85}]
    assert app.db_query("SELECT topic_id FROM topic_history") == [{"topic_id": queued[0].id}]
    assert app.sent == []
    app.recorder.record(
        "digest_truncation",
        assessment_calls=1,
        truncated_marker=True,
        queued_topics=len(queued),
        cache_rows=len(cached),
    )


async def test_short_entry_skips_assessment_model(core_app_factory, monkeypatch):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                text=KEEP_ASSESSMENT,
                when=lambda request: request.purpose == "digest_topic_fit",
                name="short_reading",
            )
        ]
    )
    await app.start()
    assert app.muika is not None

    hits: list[str] = []
    application = web.Application()
    # 标题 + 正文归一化后仅 13 个字符；trafilatura 对短页提取出 "Too short."。
    application.router.add_get("/rss", _rss_handler("Hi", "/article", hits))
    application.router.add_get("/article", _article_handler("Too short.", hits))
    async with TestClient(TestServer(application)) as client:
        _patch_sources(monkeypatch, client, {"reading": "/rss"})
        await asyncio.wait_for(app.muika.digest_agent.fetch_and_digest(), 10)

    # 条目已被真实抓取并走到内容检查，但没有进入评估模型。
    assert hits == ["/rss", "/article"]
    assert app.scripted.calls == []
    assert app.db_query("SELECT topic_id FROM rss_digest_cache") == []
    assert app.db_query("SELECT topic_id FROM topic_history") == []
    assert list(app.muika.topic_manager._event_queue) == []
    assert app.sent == []
    app.recorder.record("digest_short_entry", fetched=len(hits), assessment_calls=0, queued_topics=0)


async def test_assessment_failure_does_not_block_other_sources(core_app_factory, monkeypatch):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                text=KEEP_ASSESSMENT,
                when=lambda request: request.purpose == "digest_topic_fit" and "Second reading" in request.prompt,
                name="second_reading",
            )
        ]
    )
    await app.start()
    assert app.muika is not None

    attempts: list[str] = []
    scripted_ask = app.scripted.ask

    async def failing_assessment(request, **kwargs):
        if request.purpose == "digest_topic_fit":
            attempts.append(request.prompt)
            if "First reading" in request.prompt:
                raise RuntimeError("assessment service unavailable")
        return await scripted_ask(request, **kwargs)

    monkeypatch.setattr(app.scripted, "ask", failing_assessment)

    application = web.Application()
    application.router.add_get("/rss-first", _rss_handler("First reading", "/article-first"))
    application.router.add_get("/rss-second", _rss_handler("Second reading", "/article-second"))
    application.router.add_get("/article-first", _article_handler("word " * 800 + " first body"))
    application.router.add_get("/article-second", _article_handler("word " * 800 + " second body"))
    async with TestClient(TestServer(application)) as client:
        _patch_sources(monkeypatch, client, {"first": "/rss-first", "second": "/rss-second"})
        # 第一源评估抛错被吞掉，整轮摘要仍继续跑到第二源。
        await asyncio.wait_for(app.muika.digest_agent.fetch_and_digest(), 10)

    assert len(attempts) == 2
    assert "First reading" in attempts[0]
    assert "Second reading" in attempts[1]
    assert [row["source_id"] for row in app.db_query("SELECT source_id FROM rss_digest_cache")] == ["second"]

    queued = list(app.muika.topic_manager._event_queue)
    assert len(queued) == 1
    assert queued[0].id.startswith("event_rss_second_")
    assert queued[0].title == "Second reading"
    assert app.db_query("SELECT topic_id FROM topic_history") == [{"topic_id": queued[0].id}]
    app.recorder.record(
        "digest_assessment_failure",
        assessment_attempts=len(attempts),
        failed_source="first",
        recovered_source="second",
    )
