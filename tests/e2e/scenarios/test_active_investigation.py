"""主动调查：信息不足时她可以让 Agent 只读调查，再决定是否开口。"""

import json
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from harness import ScriptedTurn

from muika.config import mas_config
from muika.core.topic_manager import EventTopic, TopicSource
from muika.llm._schema import ToolCall

pytestmark = pytest.mark.e2e


def _clock(monkeypatch):
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 11, 7, 15, 0)
    for target in (
        "muika.core.loop.datetime",
        "muika.core.memory.datetime",
        "muika.core.topic_manager.datetime",
        "muika.database.crud.datetime",
    ):
        monkeypatch.setattr(target, clock)
    return clock


def _prepare(app, clock) -> None:
    app.muika.topic_manager.store.is_empty = lambda: True
    app.muika.topic_manager.enqueue_event(
        EventTopic(
            id="holdout-ukraine",
            source=TopicSource.EVENT,
            category="news",
            title="World news",
            content="A recent news item says the war in Ukraine is still ongoing after several years.",
        )
    )
    app.muika.state.boredom = 0.9
    app.muika.state.last_interaction = clock.now() - timedelta(minutes=30)


async def test_investigation_before_speaking(core_app_factory, monkeypatch):
    clock = _clock(monkeypatch)
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when="new proactive topic",
                name="investigate",
                text="<heart>只有一句话，我知道得太少。战争已打了好几年。</heart><agent>搜索乌克兰战争目前进展</agent>",
            ),
        ]
    )
    submitted: list[dict] = []

    async def fake_submit(instruction, original_request, *, intention_id=None, read_only=False):
        submitted.append({"instruction": instruction, "original_request": original_request, "read_only": read_only})
        return MagicMock()

    await app.start()
    monkeypatch.setattr(app.muika.agent_tasks, "submit", fake_submit)
    _prepare(app, clock)
    await app.advance_time()
    await app.wait_processed("time_tick")

    # 调查轮不发可见消息；任务只读且带素材语境；她的真实输出（含 heart）进入上下文
    assert app.sent == []
    assert submitted and submitted[0]["read_only"] is True
    assert "Reading notes" in submitted[0]["original_request"]
    assert any("战争已打了好几年" in turn.content for turn in app.muika.memory.recent_turns if turn.role == "muika")
    rows = app.db_query("SELECT COUNT(*) AS n FROM topic_history WHERE topic_id = ?", ("holdout-ukraine",))
    assert rows[0]["n"] >= 1
    app.recorder.record("active_investigation", visible=0, read_only=True, seed_has_notes=True)


async def test_investigation_results_reach_topic_prompt(core_app_factory, monkeypatch):
    clock = _clock(monkeypatch)
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when="recent investigation results",
                name="with_results",
                text="根据查到的资料，战争已经进入第三年，谈判陷入了僵局。",
            ),
        ]
    )
    await app.start()
    await app.muika.memory.add_context(
        "agent", "[Action result] Search found: negotiations stalled; fighting continues."
    )
    _prepare(app, clock)
    await app.advance_time()
    await app.wait_processed("time_tick")

    assert await app.next_reply() == "根据查到的资料，战争已经进入第三年，谈判陷入了僵局。"
    call = next(call for call in app.scripted.calls if call["name"] == "with_results")
    assert "recent investigation results" in call["prompt"]
    app.recorder.record("investigation_results_injected", prompt_has_results=True)


async def test_silence_records_topic_cooldown(core_app_factory, monkeypatch):
    clock = _clock(monkeypatch)
    app = await core_app_factory(
        turns=[ScriptedTurn(when="new proactive topic", name="silent", text="<heart>不值得说。</heart><do_nothing>")]
    )
    await app.start()
    _prepare(app, clock)
    await app.advance_time()
    await app.wait_processed("time_tick")

    assert app.sent == []
    rows = app.db_query("SELECT COUNT(*) AS n FROM topic_history WHERE topic_id = ?", ("holdout-ukraine",))
    assert rows[0]["n"] >= 1
    app.recorder.record("declined_topic_cooldown", recorded=True)


async def test_agent_disabled_investigates_with_direct_read_tools(core_app_factory, monkeypatch, tmp_path):
    clock = _clock(monkeypatch)
    source = tmp_path / "background.txt"
    source.write_text("A verified background detail.", encoding="utf-8")
    monkeypatch.setattr(mas_config, "enable_agent", False)
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])

    def check_request(request):
        if request.purpose != "topic_expand":
            return False
        assert request.tools
        assert all(tool.read_only for tool in request.tools)
        assert "write <agent>" not in request.prompt
        return True

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=check_request,
                text="",
                tool_calls=[
                    ToolCall(id="read-background", name="read_file", arguments=json.dumps({"path": str(source)}))
                ],
            ),
            ScriptedTurn(when=check_request, text="A thought based on that detail."),
        ]
    )
    await app.start()
    _prepare(app, clock)
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert await app.next_reply() == "A thought based on that detail."
    executions = [entry for entry in app.recorder.entries if entry["kind"] == "tool_exec"]
    assert len(executions) == 1
    assert executions[0]["name"] == "read_file"
    assert not executions[0]["is_error"]
    assert "A verified background detail." in executions[0]["result"]
    assert not app.muika.agent_tasks.tasks
    assert source.read_text(encoding="utf-8") == "A verified background detail."
