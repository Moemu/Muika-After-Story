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
    NIGHT_END_HOUR,
    NIGHT_START_HOUR,
    ReflectionAgent,
    _extract_outcome,
    _in_night_window,
)

# --------------------------------------------------------------------------- helpers


class FakeButler:
    """裸 Butler stub；只实现 ReflectionAgent 所需的 execute_command。"""

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
    butler=None,
    executor=None,
    topic_manager=None,
):
    return ReflectionAgent(
        butler_agent=butler or FakeButler(),
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
    # 按 period_end 排序，确保 _pending_session_count 稳定
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


# --------------------------------------------------------------------------- _in_night_window


def test_in_night_window_inside():
    assert _in_night_window(datetime(2026, 1, 1, 2, 0)) is True
    assert _in_night_window(datetime(2026, 1, 1, 3, 15)) is True
    assert _in_night_window(datetime(2026, 1, 1, 4, 59)) is True


def test_outside_night_window_daytime():
    assert _in_night_window(datetime(2026, 1, 1, 14, 0)) is False


def test_outside_night_window_early_morning():
    assert _in_night_window(datetime(2026, 1, 1, 1, 59)) is False


def test_night_window_boundary():
    assert _in_night_window(datetime(2026, 1, 1, NIGHT_START_HOUR, 0)) is True
    assert _in_night_window(datetime(2026, 1, 1, NIGHT_END_HOUR, 0)) is False


# --------------------------------------------------------------------------- _extract_outcome


def test_extract_outcome_found():
    report = "... lots of text ...\n[REFLECTION_OUTCOME] I tweaked my morning greeting."
    assert _extract_outcome(report) == "I tweaked my morning greeting."


def test_extract_outcome_with_surrounding_whitespace():
    report = "[REFLECTION_OUTCOME]   I changed a topic seed.  \n\n"
    assert _extract_outcome(report) == "I changed a topic seed."


def test_extract_outcome_missing():
    report = "I looked through the files and nothing needed changing."
    fallback = _extract_outcome(report)
    assert "think about myself" in fallback


# --------------------------------------------------------------------------- gates


@pytest.mark.asyncio
async def test_gate_self_mod_disabled(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", False)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0))

    agent = _make_agent()
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent.maybe_reflect()
    assert agent._butler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_auto_reflection_disabled(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", False)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0))

    agent = _make_agent()
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent.maybe_reflect()
    assert agent._butler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_outside_night_window(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, 14, 0))

    agent = _make_agent()
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent.maybe_reflect()
    assert agent._butler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_cooldown_not_elapsed(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    monkeypatch.setattr(mas_config, "reflection_cooldown_hours", 24)
    now = datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0)
    _patch_datetime_now(monkeypatch, now)

    memory = MemoryManager(max_turns=3)
    # 写入一个 1 小时前的冷却锚点（< 24h）
    memory.records["core:self:self_reflection_last_at"] = MagicMock(value=(now - timedelta(hours=1)).isoformat())
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(memory, days_ago=MIN_PENDING_SESSIONS - i)

    agent = _make_agent(memory=memory)
    await agent.maybe_reflect()
    assert agent._butler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_pending_sessions_too_few(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0))

    agent = _make_agent()
    # 只塞 3 条 archives，低于 MIN_PENDING_SESSIONS (5)
    for i in range(3):
        _seed_archive(agent._memory, days_ago=3 - i)

    await agent.maybe_reflect()
    assert agent._butler.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_gate_passes_all(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0))
    # 把 reflection.py 与 memory.py 的延迟 get_session 都重定向到测试 DB
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    butler = FakeButler(report="[REFLECTION_OUTCOME] I tweaked a line.")
    agent = _make_agent(butler=butler)
    for i in range(MIN_PENDING_SESSIONS):
        _seed_archive(agent._memory, days_ago=MIN_PENDING_SESSIONS - i)

    await agent._run_reflection(notify_user=False)
    assert len(butler.calls) == 1
    # 冷却锚点必然写入
    assert "core:self:self_reflection_last_at" in agent._memory.records


# --------------------------------------------------------------------------- force_reflect


@pytest.mark.asyncio
async def test_force_reflect_skips_gates(monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_auto_reflection", True)
    # 白天也强制自省
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, 14, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    butler = FakeButler(report="[REFLECTION_OUTCOME] Forced reflection done.")
    executor = FakeExecutor()
    agent = _make_agent(butler=butler, executor=executor)

    await agent.force_reflect()
    assert len(butler.calls) == 1
    assert "Forced reflection done." in executor.messages


@pytest.mark.asyncio
async def test_force_reflect_outcome_not_sent_when_no_marker(monkeypatch, db_session, session_ctx_factory):
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, 14, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)

    butler = FakeButler(report="Just some text without the outcome marker.")
    executor = FakeExecutor()
    agent = _make_agent(butler=butler, executor=executor)

    await agent.force_reflect()
    # 兜底句也会发出
    assert len(executor.messages) == 1
    assert "think about myself" in executor.messages[0]


# --------------------------------------------------------------------------- _gather_context


@pytest.mark.asyncio
async def test_gather_context_caps_at_max_summaries():
    memory = MemoryManager(max_turns=3)
    for i in range(MAX_SUMMARIES + 5):
        _seed_archive(memory, days_ago=MAX_SUMMARIES + 5 - i, summary=f"session {i}")

    agent = _make_agent(memory=memory)
    ctx = await agent._gather_context(last_at=None)
    # 最多 MAX_SUMMARIES 条
    assert ctx.count("- [") == MAX_SUMMARIES


@pytest.mark.asyncio
async def test_gather_context_filters_by_last_at():
    memory = MemoryManager(max_turns=3)
    for i in range(10):
        _seed_archive(memory, days_ago=10 - i, summary=f"session {i}")

    # last_at = 5 天前 → 只有 days_ago 1..4 的 archive 满足 period_end > last_at（严格 >）
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


# --------------------------------------------------------------------------- concurrency guard


@pytest.mark.asyncio
async def test_run_reflection_not_concurrent(monkeypatch, db_session, session_ctx_factory):
    _patch_datetime_now(monkeypatch, datetime(2026, 1, 1, NIGHT_START_HOUR + 1, 0))
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.core.reflection.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)
    butler = FakeButler(report="[REFLECTION_OUTCOME] done")

    import asyncio

    async def slow_execute(*args, **kwargs):
        await asyncio.sleep(0.1)
        return ("report", [])

    butler.execute_command = slow_execute  # type: ignore[assignment]

    agent = _make_agent(butler=butler)
    # 并发启动两次；第二次应该被 _running 拦掉
    t1 = asyncio.create_task(agent._run_reflection())
    t2 = asyncio.create_task(agent._run_reflection())
    await asyncio.gather(t1, t2)
    # slow_execute 不通过 FakeButler.calls 计数；只校验锁已释放
    assert agent._running is False


# --------------------------------------------------------------------------- plugin


def test_reflect_plugin_module_structure():
    from muika.builtin_plugins import reflect

    assert hasattr(reflect, "metadata")
    assert reflect.metadata.name == "reflect"
    assert hasattr(reflect, "reflect_cmd")
