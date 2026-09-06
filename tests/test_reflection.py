"""ReflectionAgent 单元测试：门控逻辑、执行流、outcome 提取、``.reflect`` 插件注册。"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from muika.config import mas_config
from muika.core.memory import ArchiveEntry, MemoryManager
from muika.core.reflection import (
    MAX_SUMMARIES,
    MIN_PENDING_SESSIONS,
    REFLECTION_HOUR,
    ReflectionAgent,
    _extract_outcome,
    seconds_until_reflection,
)


class FakeAgent:
    """裸 Agent stub；只实现 ReflectionAgent 所需的 execute_command。"""

    def __init__(self, report: str = "") -> None:
        self.report = report
        self.calls: list[str] = []

    async def execute_command(self, command, state, executor):  # noqa: D401
        self.calls.append(command)
        return (self.report, [])


class FakeExecutor:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message(self, message, resources=None, target=None):  # noqa: D401
        self.messages.append(message)


class FakeTopicManager:
    async def get_engagement_stats(self, top_n=10):  # noqa: D401
        return "- t1 (philosophy): used 5x, engaged 3x (60%)"


def _make_agent(
    memory=None,
    action_agent=None,
    executor=None,
    topic_manager=None,
):
    return ReflectionAgent(
        agent=action_agent or FakeAgent(),
        memory=memory or MemoryManager(max_turns=3),
        state=MagicMock(),
        topic_manager=topic_manager or FakeTopicManager(),
        executor=executor or FakeExecutor(),
    )


def _seed_archive(memory: MemoryManager, days_ago: int, summary: str = "session happened") -> None:
    """在 ``memory.archives`` 中塞一条 days_ago 天前的摘要。"""
    now = datetime.now()
    archive = ArchiveEntry(
        session_id=f"s_{days_ago}",
        summary=summary,
        period_start=now - timedelta(days=days_ago, hours=1),
        period_end=now - timedelta(days=days_ago),
    )
    memory.archives.append(archive)
    memory.archives.sort(key=lambda a: a.period_end)


class _FakeDatetime(datetime):
    """可控 now() 的 datetime 子类；测试用 monkeypatch 注入到 reflection 模块。"""

    _now_value: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # noqa: D401
        if cls._now_value is None:
            return datetime.now(tz) if tz else datetime.now()
        return cls._now_value


def _patch_datetime_now(monkeypatch, value: datetime) -> None:
    """把 reflection 模块内的 datetime 替换为 _FakeDatetime，固定 now() 返回值。"""
    _FakeDatetime._now_value = value
    monkeypatch.setattr("muika.core.reflection.datetime", _FakeDatetime)


def test_seconds_until_reflection_before_daily_time():
    now = datetime(2026, 1, 1, REFLECTION_HOUR - 1, 0)

    assert seconds_until_reflection(now) == 3600


def test_seconds_until_reflection_after_daily_time():
    now = datetime(2026, 1, 1, REFLECTION_HOUR + 1, 0)

    assert seconds_until_reflection(now) == 23 * 3600


def test_extract_outcome_uses_plain_report():
    report = "  I left my persona unchanged because recent conversations felt natural.  \n"
    assert _extract_outcome(report) == "I left my persona unchanged because recent conversations felt natural."


def test_extract_outcome_does_not_require_marker():
    report = "I looked through the files and nothing needed changing."
    assert _extract_outcome(report) == report


@pytest.mark.asyncio
async def test_gate_self_mod_disabled(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", False)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, REFLECTION_HOUR, 0))

    agent = _make_agent()
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent.maybe_reflect()
    assert agent._agent.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_auto_reflection_disabled(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", False)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, REFLECTION_HOUR, 0))

    agent = _make_agent()
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent.maybe_reflect()
    assert agent._agent.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_cooldown_not_elapsed(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    monkeypatch.setattr(mas_config, "reflection_cooldown_hours", 24)
    now = datetime(2026, 1, 1, REFLECTION_HOUR, 0)
    _patch_datetime_now(monkeypatch, now)

    memory = MemoryManager(max_turns=3)
    memory.records["core:self:self_reflection_last_at"] = MagicMock(value=(now - timedelta(hours=1)).isoformat())
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(memory, days_ago=MIN_PENDING_SESSIONS - i)

    agent = _make_agent(memory=memory)
    await agent.maybe_reflect()
    assert agent._agent.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_pending_sessions_too_few(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, REFLECTION_HOUR, 0))

    agent = _make_agent()
    for i in range(3):
        _seed_archive(agent._memory, days_ago=3 - i)

    await agent.maybe_reflect()
    assert agent._agent.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_passes_all(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, REFLECTION_HOUR, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    action_agent = FakeAgent(report="I left my persona unchanged.")
    agent = _make_agent(action_agent=action_agent)
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent._run_reflection(notify_user=False)
    assert len(action_agent.calls) == 1
    assert "core:self:self_reflection_last_at" in agent._memory.records


@pytest.mark.asyncio
async def test_force_reflect_skips_gates(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, 14, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    action_agent = FakeAgent(report="Forced reflection done.")
    executor = FakeExecutor()
    agent = _make_agent(action_agent=action_agent, executor=executor)

    await agent.force_reflect()
    assert len(action_agent.calls) == 1
    assert "Forced reflection done." in executor.messages


@pytest.mark.asyncio
async def test_force_reflect_sends_plain_report(monkeypatch, db_session, session_ctx_factory):
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, 14, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    action_agent = FakeAgent(report="Just some text without the outcome marker.")
    executor = FakeExecutor()
    agent = _make_agent(action_agent=action_agent, executor=executor)

    await agent.force_reflect()
    assert len(executor.messages) == 1
    assert executor.messages[0] == "Just some text without the outcome marker."


@pytest.mark.asyncio
async def test_gather_context_caps_at_max_summaries():
    memory = MemoryManager(max_turns=3)
    for i in range(MAX_SUMMARIES + 5):
        _seed_archive(memory, days_ago=MAX_SUMMARIES + 5 - i, summary=f"session {i}")

    agent = _make_agent(memory=memory)
    ctx = await agent._gather_context(last_at=None)
    assert ctx.count("- [") == MAX_SUMMARIES


@pytest.mark.asyncio
async def test_gather_context_filters_by_last_at():
    memory = MemoryManager(max_turns=3)
    for i in range(10):
        _seed_archive(memory, days_ago=10 - i, summary=f"session {i}")

    last_at = datetime.now() - timedelta(days=5)
    agent = _make_agent(memory=memory)
    ctx = await agent._gather_context(last_at=last_at)
    assert ctx.count("- [") == 4


@pytest.mark.asyncio
async def test_gather_context_empty():
    memory = MemoryManager(max_turns=3)
    agent = _make_agent(memory=memory)
    ctx = await agent._gather_context(last_at=None)
    assert "no recent session summaries" in ctx


@pytest.mark.asyncio
async def test_run_reflection_not_concurrent(monkeypatch, db_session, session_ctx_factory):
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, REFLECTION_HOUR, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)
    action_agent = FakeAgent(report="done")

    import asyncio

    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(0.1)
        return ("report", [])

    action_agent.execute_command = slow_execute  # type: ignore[assignment]

    agent = _make_agent(action_agent=action_agent)
    t1 = asyncio.create_task(agent._run_reflection())
    t2 = asyncio.create_task(agent._run_reflection())
    await asyncio.gather(t1, t2)
    assert agent._running is False


def test_reflect_plugin_module_structure():
    from muika.builtin_plugins import reflect

    assert hasattr(reflect, "metadata")
    assert reflect.metadata.name == "reflect"
    assert hasattr(reflect, "reflect_cmd")
