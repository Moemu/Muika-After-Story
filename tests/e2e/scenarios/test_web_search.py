"""联网搜索真实性：Agent 通过真实管线调用 web_search，结果回到她的可见回复。"""

import json

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.core.actions.tools import _search
from muika.core.actions.tools._search import SearchResult
from muika.core.agent.task_store import TaskStore
from muika.llm import ModelRequest
from muika.llm._schema import ToolCall

pytestmark = pytest.mark.e2e

RELEASE_URL = "https://example.com/mas-release"
RELEASE_NOTE = "Muika-After-Story 1.2.0 released with web search."


def _is_agent_request(request: ModelRequest) -> bool:
    """Agent 半身的请求带工具清单；主人格（非上帝模式）不带。"""
    return bool(request.tools)


async def test_agent_web_search_is_grounded_and_recorded(core_app_factory, recorder, monkeypatch):
    calls: list[tuple[str, str | None]] = []

    async def fake_tavily(query: str, time_range: str | None) -> list[SearchResult]:
        calls.append((query, time_range))
        return [
            SearchResult(
                title="MAS release",
                url=RELEASE_URL,
                snippet=RELEASE_NOTE,
                date="2026-09-27",
            )
        ]

    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "web_search_provider", "tavily")
    monkeypatch.setattr(mas_config, "web_search_api_key", "test-key")
    monkeypatch.setitem(_search.SEARCH_BACKENDS, "tavily", fake_tavily)

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text="稍等，我去查一下最新动态。<agent>搜索 Muika-After-Story 最近一周的版本发布动态，告诉我发了什么。</agent>",
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_search",
                text="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="web_search",
                        arguments=json.dumps({"query": "Muika-After-Story release", "time_range": "week"}),
                    )
                ],
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text='<agent_result status="completed">'
                + json.dumps(
                    {
                        "summary": f"Found release news: {RELEASE_NOTE} ({RELEASE_URL})",
                        "verification": ["web_search returned results"],
                    }
                )
                + "</agent_result>",
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="查到啦！1.2.0 更新发布了，还带来了联网搜索呢。",
            ),
        ]
    )
    await app.start()

    await app.user_says("帮我看看 MAS 最近有什么更新？")
    ack = await app.next_reply()
    # <agent> 委派指令不外泄
    assert ack == "稍等，我去查一下最新动态。"
    assert_clean_visible(ack)

    final = await app.next_reply(timeout=20)
    assert_clean_visible(final)
    await app.wait_processed("agent_task")

    # 真实性不变量一：后端确实收到了模型发起的查询参数
    assert calls == [("Muika-After-Story release", "week")]

    # 真实性不变量二：工具经真实管线执行，结果回流到下一步输入
    steps = [
        entry
        for entry in recorder.entries
        if entry["kind"] == "llm_call" and entry["name"] in {"agent_search", "agent_report"}
    ]
    assert [step["tool_calls"] for step in steps] == [["web_search"], []]
    assert steps[1]["saw"].startswith("tool:") and RELEASE_URL in steps[1]["saw"]

    # 真实性不变量三：工具调用持久化为 completed
    persisted = [
        {"status": call.status, "payload": call.model_dump_json()}
        for task in await TaskStore().load()
        for call in await TaskStore().calls(task.id)
    ]
    assert [row["status"] for row in persisted] == ["completed"]
    assert "web_search" in persisted[0]["payload"]

    # 全链路恰好两轮可见回复，剧本耗尽，无多余 LLM 调用
    assert app.sent == [ack, final]
    assert app.scripted.pending_turns == 0


async def test_unconfigured_web_search_reports_unavailability(core_app_factory, recorder, monkeypatch):
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "web_search_provider", "")
    monkeypatch.setattr(mas_config, "web_search_api_key", "")

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text="我想想……<agent>搜索一下今天的科技新闻。</agent>",
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_search",
                text="",
                tool_calls=[
                    ToolCall(id="call-1", name="web_search", arguments=json.dumps({"query": "tech news today"}))
                ],
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text='<agent_result status="completed">'
                + json.dumps(
                    {
                        "summary": "Web search is not configured on this machine",
                        "verification": ["web_search reported unavailability"],
                    }
                )
                + "</agent_result>",
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="这台机器还没给我配搜索的钥匙，先不查啦。",
            ),
        ]
    )
    await app.start()

    await app.user_says("帮我搜搜今天的新闻")
    ack = await app.next_reply()
    final = await app.next_reply(timeout=20)
    assert_clean_visible(ack)
    assert_clean_visible(final)
    await app.wait_processed("agent_task")

    # 未配置时工具返回明确的不可用说明并回流给模型，而不是抛错或编造结果
    steps = [
        entry
        for entry in recorder.entries
        if entry["kind"] == "llm_call" and entry["name"] in {"agent_search", "agent_report"}
    ]
    assert [step["tool_calls"] for step in steps] == [["web_search"], []]
    assert steps[1]["saw"].startswith("tool:") and "not configured" in steps[1]["saw"]

    persisted = [
        {"status": call.status, "payload": call.model_dump_json()}
        for task in await TaskStore().load()
        for call in await TaskStore().calls(task.id)
    ]
    assert [row["status"] for row in persisted] == ["completed"]
    assert "web_search" in persisted[0]["payload"]

    assert app.sent == [ack, final]
    assert app.scripted.pending_turns == 0
