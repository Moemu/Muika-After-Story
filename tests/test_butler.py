"""``ButlerAgent`` 四方法测试——双裸 ``FakeLLM`` stub 替换 model / summarize_model。

``ButlerAgent.__new__`` 绕过构造（避免 ``load_model`` / SkillManager watcher），
工具列表与模板渲染被 mock。
"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from muika.core.butler.agent import ButlerAgent
from muika.core.memory import (
    MemoryCategory,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
    SessionTurn,
)
from muika.core.state import MuikaState
from muika.llm import ModelCompletions


def _butler(fake_model, fake_summarize=None) -> ButlerAgent:
    agent = ButlerAgent.__new__(ButlerAgent)
    agent.model = fake_model
    agent.summarize_model = fake_summarize or fake_model
    agent._skill_manager = cast(Any, SimpleNamespace(render_prompt_section=lambda: ""))
    return agent


def _preference() -> MemoryRecord:
    return MemoryRecord(layer=MemoryLayer.PREFERENCE, category=MemoryCategory.USER, key="fav_drink", value="tea")


# ---------------------------------------------------------------------------
# fetch_relevant_preferences
# ---------------------------------------------------------------------------


async def test_fetch_preferences_empty_shortcircuit(fake_llm_factory):
    agent = _butler(fake_llm_factory())
    result = await agent.fetch_relevant_preferences("hi", [])
    assert result == []
    assert agent.model.call_count == 0


async def test_fetch_preferences_returns_matched(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='{"relevant_keys":["fav_drink"]}'))
    agent = _butler(fake)
    pref = _preference()
    result = await agent.fetch_relevant_preferences("drink?", [pref])
    assert result == [pref]
    assert agent.model.requests[0].format == "json"


async def test_fetch_preferences_llm_failure_empty(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    agent = _butler(fake)
    result = await agent.fetch_relevant_preferences("hi", [_preference()])
    assert result == []


# ---------------------------------------------------------------------------
# summarize_session
# ---------------------------------------------------------------------------


async def test_summarize_session_empty(fake_llm_factory):
    agent = _butler(fake_llm_factory())
    result = await agent.summarize_session([])
    assert result == ""
    assert agent.summarize_model.call_count == 0


async def test_summarize_session_returns_stripped(fake_llm_factory):
    summarize = fake_llm_factory(response=ModelCompletions(text="A good day. <think>x</think>"))
    agent = _butler(fake_llm_factory(), fake_summarize=summarize)
    turns = [SessionTurn(role="user", content="hello"), SessionTurn(role="muika", content="hi")]
    result = await agent.summarize_session(turns)
    assert result == "A good day."
    assert agent.model.call_count == 0  # 走 summarize_model
    assert summarize.call_count == 1


async def test_summarize_session_llm_failure(fake_llm_factory):
    summarize = fake_llm_factory(error=RuntimeError("boom"))
    agent = _butler(fake_llm_factory(), fake_summarize=summarize)
    with pytest.raises(RuntimeError, match="boom"):
        await agent.summarize_session([SessionTurn(role="user", content="x")])


# ---------------------------------------------------------------------------
# classify_and_store_memory
# ---------------------------------------------------------------------------


async def test_classify_empty_content(fake_llm_factory):
    agent = _butler(fake_llm_factory())
    await agent.classify_and_store_memory("   ", MuikaState())
    assert agent.model.call_count == 0


async def test_classify_state_memory_none(fake_llm_factory):
    fake = fake_llm_factory(
        response=ModelCompletions(
            text='{"should_store":true,"layer":"core","category":"user","key":"fav_drink","value":"tea"}'
        )
    )
    agent = _butler(fake)
    state = MuikaState()  # memory 未注入
    await agent.classify_and_store_memory("likes tea", state)
    assert agent.model.call_count == 1  # 解析成功但 state.memory 为 None 时不写


async def test_classify_stores_record(fake_llm_factory, redirect_get_session):
    fake = fake_llm_factory(
        response=ModelCompletions(
            text='{"should_store":true,"layer":"core","category":"user","key":"fav_drink","value":"tea"}'
        )
    )
    agent = _butler(fake)
    state = MuikaState(memory=MemoryManager())
    await agent.classify_and_store_memory("likes tea", state)
    assert "core:user:fav_drink" in state.memory.records
    assert state.memory.records["core:user:fav_drink"].value == "tea"


async def test_classify_can_decline_task_storage(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='{"should_store":false,"reason":"task plan"}'))
    agent = _butler(fake)
    memory = MemoryManager()

    await agent.classify_and_store_memory("请实现一个插件", MuikaState(memory=memory))

    assert memory.records == {}


async def test_classify_retries_then_gives_up(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    agent = _butler(fake)
    state = MuikaState(memory=MemoryManager())
    await agent.classify_and_store_memory("x", state, max_retry=3)
    assert fake.call_count == 4  # 初始 + 3 次重试
    assert len(state.memory.records) == 0


# ---------------------------------------------------------------------------
# execute_command
# ---------------------------------------------------------------------------


def _cmd_patches():
    return (
        patch("muika.core.butler.agent.get_tool_list", return_value=[{"name": "read_file"}]),
        patch("muika.core.butler.agent.generate_prompt_from_template", return_value="SYSTEM"),
    )


async def test_execute_command_report_and_resources(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _butler(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, resources = await agent.execute_command("test", MuikaState(), executor=None)
    assert report == "Done."
    assert resources == []
    req = agent.model.requests[0]
    assert req.prompt == "Command: test"
    assert req.tools == [{"name": "read_file"}]


async def test_execute_command_system_assembly(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _butler(fake)
    agent._skill_manager = cast(Any, SimpleNamespace(render_prompt_section=lambda: "SKILLS"))
    with _cmd_patches()[0], _cmd_patches()[1]:
        await agent.execute_command("cmd", MuikaState(), executor=None)
    assert agent.model.requests[0].system == "SYSTEM\n\nSKILLS"


async def test_execute_command_llm_error(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    agent = _butler(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, resources = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report.startswith("I encountered an error")
    assert resources == []


async def test_execute_command_clears_context(fake_llm_factory):
    from muika.plugin.func_call._context import get_state

    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _butler(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        await agent.execute_command("cmd", MuikaState(), executor=None)
    assert get_state() is None


async def test_execute_command_rejects_acknowledgement_as_result(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="好的，我先读取源码。"))
    agent = _butler(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, _ = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report.startswith("Agent stopped before reporting completion.")


async def test_execute_command_reports_blocked_status(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="blocked">No access.</agent_result>'))
    agent = _butler(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, _ = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report == "Agent blocked: No access."
