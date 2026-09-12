"""``Agent`` 四方法测试——双裸 ``FakeLLM`` stub 替换 model / summarize_model。

``Agent.__new__`` 绕过构造（避免 ``load_model`` / SkillManager watcher），
工具列表与模板渲染被 mock。
"""

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from muika.config import mas_config
from muika.core.agent.agent import Agent
from muika.core.memory_reasoning import MemoryReasoner
from muika.core.state import MuikaState
from muika.llm import ModelCompletions, ModelConfig


@pytest.fixture(autouse=True)
def model_configs(monkeypatch):
    monkeypatch.setattr("muika.core.agent.agent.get_model_config", lambda name: ModelConfig(provider="_echo"))


def _agent(fake_model, fake_summarize=None) -> Agent:
    agent = Agent.__new__(Agent)
    agent.action_lock = asyncio.Lock()
    agent.model = fake_model
    agent.summarize_model = fake_summarize or fake_model
    agent.memory_reasoner = MemoryReasoner(agent.model, agent.summarize_model)
    agent._skill_manager = cast(Any, SimpleNamespace(render_prompt_section=lambda: ""))
    return agent


def _cmd_patches():
    return (
        patch("muika.core.agent.agent.get_tool_list", return_value=[{"name": "read_file"}]),
        patch("muika.core.agent.agent.generate_prompt_from_template", return_value="SYSTEM"),
    )


async def test_execute_command_report_and_resources(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _agent(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, resources = await agent.execute_command("test", MuikaState(), executor=None)
    assert report == "Done."
    assert resources == []
    req = agent.model.requests[0]
    assert req.prompt == "Command: test"
    assert req.tools == [{"name": "read_file"}]


async def test_execute_command_system_assembly(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _agent(fake)
    agent._skill_manager = cast(Any, SimpleNamespace(render_prompt_section=lambda: "SKILLS"))
    with _cmd_patches()[0], _cmd_patches()[1]:
        await agent.execute_command("cmd", MuikaState(), executor=None)
    assert agent.model.requests[0].system.startswith("SYSTEM\n\nSKILLS\n\nExecution environment:")


async def test_execute_command_llm_error(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    agent = _agent(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, resources = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report.startswith("I encountered an error")
    assert resources == []


async def test_execute_command_clears_context(fake_llm_factory):
    from muika.plugin.func_call.context import get_dependencies

    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">Done.</agent_result>'))
    agent = _agent(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        await agent.execute_command("cmd", MuikaState(), executor=None)
    assert get_dependencies()[MuikaState] is None


async def test_execute_command_rejects_acknowledgement_as_result(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="好的，我先读取源码。"))
    agent = _agent(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, _ = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report.startswith("Agent stopped before reporting completion.")


async def test_execute_command_reports_blocked_status(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="blocked">No access.</agent_result>'))
    agent = _agent(fake)
    with _cmd_patches()[0], _cmd_patches()[1]:
        report, _ = await agent.execute_command("cmd", MuikaState(), executor=None)
    assert report == "Agent blocked: No access."


def test_named_models_refresh_at_request_boundary_including_summary_only_changes(monkeypatch, fake_llm_factory):
    action = fake_llm_factory()
    summary = fake_llm_factory()
    agent = _agent(action, summary)
    monkeypatch.setattr(mas_config, "agent_model", "action")
    monkeypatch.setattr(mas_config, "session_summarize_model", "summary")
    configs = {"action": action.config, "summary": summary.config}
    monkeypatch.setattr("muika.core.agent.agent.get_model_config", configs.__getitem__)

    def load(config):
        model = fake_llm_factory()
        model.config = config
        return model

    monkeypatch.setattr("muika.llm.loader.load_model", load)
    with _cmd_patches()[0], _cmd_patches()[1]:
        agent.build_request("unchanged")
        assert agent.model is action and agent.summarize_model is summary
        configs["summary"] = summary.config.model_copy(update={"context_window": 200000})
        agent.build_request("summary changed")
        assert agent.model is action and agent.summarize_model is not summary
        assert agent.memory_reasoner.compactor.model.config.context_window == 200000
        assert summary.config.context_window == 131072
        configs["action"] = action.config.model_copy(update={"context_window": 1000000})
        agent.build_request("action changed")
    assert agent.model is not action and action.config.context_window == 131072
    assert agent.memory_reasoner.model is agent.model
    assert agent.memory_reasoner.summarize_model is agent.summarize_model
    assert agent.model.compactor is agent.memory_reasoner.compactor
