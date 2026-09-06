"""``Muika._parse_reply_tags``（标签解析）与 ``get_think_mode``（认知管线选择）测试。

``get_think_mode`` 用 ``__new__`` 构造实例，绕开 ``__init__`` 触发的 LLM/DB 加载。
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from muika.core.events import TimeTickEvent, UserMessageEvent, UserMessagePayload
from muika.core.loop import Muika, ParsedReply
from muika.core.state import ActiveTopicState, MuikaState
from muika.llm import ModelCompletions, ModelRequest
from muika.models import Message


def test_parse_no_tags():
    r = Muika._parse_reply_tags("Hello there")
    assert r == ParsedReply(clean_reply="Hello there", memory_contents=[], agent_commands=[], target=None)


def test_parse_memory_tag():
    r = Muika._parse_reply_tags("<memory>She likes tea</memory>")
    assert r.memory_contents == ["She likes tea"]
    assert r.clean_reply == ""


def test_parse_multiple_memories():
    r = Muika._parse_reply_tags("<memory>one</memory> <memory>two</memory>")
    assert r.memory_contents == ["one", "two"]


def test_parse_memory_empty_stripped():
    r = Muika._parse_reply_tags("<memory>   </memory>")
    assert r.memory_contents == []


def test_parse_agent_command():
    r = Muika._parse_reply_tags("<agent> run cmd </agent>")
    assert r.agent_commands == ["run cmd"]
    assert r.clean_reply == ""


def test_parse_agent_case_insensitive():
    r = Muika._parse_reply_tags("<AGENT>xyz</AGENT>")
    assert r.agent_commands == ["xyz"]


def test_parse_target():
    r = Muika._parse_reply_tags("Hi <target: qq>")
    assert r.target == "qq"
    assert r.clean_reply == "Hi"


def test_parse_target_last_wins():
    r = Muika._parse_reply_tags("<target: a> <target: b>")
    assert r.target == "b"


def test_parse_timeout():
    assert Muika._parse_reply_tags("<timeout: 10min>").timeout == 600.0
    assert Muika._parse_reply_tags("<timeout: 2h>").timeout == 7200.0


def test_parse_timeout_unrecognized_ignored():
    r = Muika._parse_reply_tags("<timeout: someday>")
    assert r.timeout is None
    assert r.clean_reply == ""


def test_parse_god_mode_variants():
    assert Muika._parse_reply_tags("<enable_god_mode>").god_mode is True
    assert Muika._parse_reply_tags("<enable_god_mode/>").god_mode is True


def test_parse_heart_stripped():
    r = Muika._parse_reply_tags("<heart>secret thoughts</heart>hello")
    assert r == ParsedReply(
        clean_reply="hello",
        memory_contents=[],
        agent_commands=[],
        target=None,
        heart_cot=["secret thoughts"],
    )


def test_parse_do_nothing():
    r = Muika._parse_reply_tags("<do_nothing>")
    assert r.do_nothing is True
    assert r.clean_reply == ""


def test_parse_heart_before_memory_ordering():
    r = Muika._parse_reply_tags("<heart>h</heart><memory>m</memory><do_nothing>")
    assert r.memory_contents == ["m"]
    assert r.do_nothing is True
    assert r.heart_cot == ["h"]
    assert r.clean_reply == ""


def test_parse_combined():
    r = Muika._parse_reply_tags(
        "Hello <memory>m</memory> <agent>cmd</agent> <target: t> <timeout: 1h> <enable_god_mode>"
    )
    assert r == ParsedReply(
        clean_reply="Hello",
        memory_contents=["m"],
        agent_commands=["cmd"],
        target="t",
        timeout=3600.0,
        god_mode=True,
    )


def _engine() -> Muika:
    m = Muika.__new__(Muika)
    m.state = MuikaState()
    return m


def _user_event() -> UserMessageEvent:
    return UserMessageEvent(payload=UserMessagePayload(message=Message(message="hi")))


def _tick() -> TimeTickEvent:
    return TimeTickEvent()


def test_think_mode_user_message_emotional():
    assert _engine().get_think_mode(_user_event()) == "emotional"


def test_think_mode_idle_tick_none():
    assert _engine().get_think_mode(_tick()) is None


def test_think_mode_active_topic_blocks():
    m = _engine()
    m.state.active_topic = ActiveTopicState(topic_id="t", topic_seed="s", topic_type="trivia")
    assert m.get_think_mode(_tick()) is None


def test_think_mode_loneliness_emotional():
    m = _engine()
    m.state.loneliness = 0.9
    assert m.get_think_mode(_tick()) == "emotional"


def test_think_mode_loneliness_cooldown_blocks():
    m = _engine()
    m.state.loneliness = 0.9
    m.state.last_proactive_at = datetime.now() - timedelta(seconds=60)
    assert m.get_think_mode(_tick()) is None


def test_think_mode_boredom_topic():
    m = _engine()
    m.state.boredom = 0.61
    assert m.get_think_mode(_tick()) == "topic"


def test_think_mode_curiosity_topic():
    m = _engine()
    m.state.curiosity = 0.7
    with patch("muika.core.loop.random", return_value=0.0):
        assert m.get_think_mode(_tick()) == "topic"
    assert m.state.curiosity == 0.0


def test_think_mode_loneliness_overrides_boredom():
    m = _engine()
    m.state.loneliness = 0.9
    m.state.boredom = 0.9
    assert m.get_think_mode(_tick()) == "emotional"


@pytest.mark.parametrize("opening", ["<heart>", "<HEART>"])
def test_incomplete_heart_hides_private_text_and_commands(opening):
    parsed = Muika._parse_reply_tags(f"Hello{opening}private<agent>run</agent><memory>secret</memory><enable_god_mode>")
    assert parsed.clean_reply == "Hello"
    assert parsed.agent_commands == []
    assert parsed.memory_contents == []
    assert not parsed.god_mode


@pytest.fixture
def engine(monkeypatch):
    for name in ("MuikaBrain", "Agent", "TopicManager", "DigestAgent", "ReflectionAgent"):
        monkeypatch.setattr(f"muika.core.loop.{name}", MagicMock())
    return Muika(MagicMock(send_message=AsyncMock()), asyncio.Queue())


async def test_god_mode_enables_tools_and_isolates_resources(engine):
    from muika.models import Resource
    from muika.plugin.func_call.context import ToolContext, get_dependencies

    requests = []
    resource = Resource(type="image", path="capture.png", mimetype="image/png")

    async def generate_reply(**kwargs):
        requests.append(kwargs["god_mode"])
        context = get_dependencies()[ToolContext]
        assert isinstance(context, ToolContext)
        assert context.state is engine.state
        assert context.executor is engine.executor
        assert context.resources == []
        if len(requests) == 1:
            return "<heart>I want to act.</heart><enable_god_mode>"
        if len(requests) == 2:
            context.resources.append(resource)
        return "Done."

    engine.brain.generate_reply = generate_reply
    await engine._run_brain_pipeline(TimeTickEvent(), [])
    handoff = await asyncio.wait_for(engine.event_queue.get(), timeout=1)
    await engine._process_event(handoff, 0)
    await engine._run_brain_pipeline(TimeTickEvent(), [])
    assert requests == [False, True, True]
    assert engine.executor.send_message.await_args_list[0].kwargs["resources"] == [resource]
    assert engine.executor.send_message.await_args_list[1].kwargs["resources"] == []
    assert get_dependencies()[ToolContext] is None


async def test_chat_and_session_end_keep_background_task(engine, monkeypatch, db_session, session_ctx_factory):
    monkeypatch.setattr("muika.core.agent.task_store.get_session", lambda: session_ctx_factory(db_session))
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step(request, messages):
        entered.set()
        await release.wait()
        return ModelCompletions(text='<agent_result status="completed">Verified.</agent_result>')

    engine.agent.action_lock = asyncio.Lock()
    engine.agent.build_request = lambda command: ModelRequest(command, tools=[])
    engine.agent.model.step = step
    engine.brain.generate_reply = AsyncMock(side_effect=["我去看看。<agent>Develop Daily</agent>", "我在呢。"])
    worker = asyncio.create_task(engine.agent_tasks.run())
    try:
        await engine._run_brain_pipeline(
            UserMessageEvent(payload=UserMessagePayload(message=Message(message="开始"))), []
        )
        await asyncio.wait_for(entered.wait(), 1)
        task = next(iter(engine.agent_tasks.tasks.values()))
        await asyncio.wait_for(
            engine._run_brain_pipeline(
                UserMessageEvent(payload=UserMessagePayload(message=Message(message="陪我聊会儿"))), []
            ),
            1,
        )
        assert engine.executor.send_message.await_args.args[0] == "我在呢。"
        await engine._handle_session_end()
        assert engine.agent_tasks.tasks[task.id] is task
        assert task.status == "running"
        release.set()
        result = await asyncio.wait_for(engine.event_queue.get(), 1)
        assert result.task_id == task.id and result.status == "completed"
    finally:
        release.set()
        await engine.agent_tasks.close()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


async def test_failed_summary_preserves_session_and_can_retry(engine, monkeypatch):
    from muika.core.constants import AUTO_SUMMARY_INTERVAL, AUTO_SUMMARY_MIN_TURNS

    clock = [1000.0]
    monkeypatch.setattr("muika.core.loop.time.monotonic", lambda: clock[0])
    for index in range(AUTO_SUMMARY_MIN_TURNS):
        engine.memory.add_context("user", str(index))
    turns = list(engine.memory.recent_turns)
    session_id = engine.memory.session.session_id
    engine.agent.summarize_session = AsyncMock(side_effect=RuntimeError("summary unavailable"))
    engine.memory.update_archive = AsyncMock()
    await engine._handle_session_end()
    assert list(engine.memory.recent_turns) == turns
    assert engine.memory.session.session_id == session_id
    assert engine._last_summary_turn is None
    engine.memory.update_archive.assert_not_awaited()
    await engine._handle_session_end()
    assert not await engine.update_session_memory()
    engine.agent.summarize_session.assert_awaited_once()
    clock[0] += AUTO_SUMMARY_INTERVAL

    engine.agent.summarize_session.side_effect = None
    engine.agent.summarize_session.return_value = "A shared memory."
    engine.memory.update_archive.side_effect = RuntimeError("database unavailable")
    await engine._handle_session_end()
    assert list(engine.memory.recent_turns) == turns
    assert engine._last_summary_turn is None

    engine.memory.update_archive.side_effect = None
    clock[0] += AUTO_SUMMARY_INTERVAL
    await engine._handle_session_end()
    assert not engine.memory.recent_turns
    assert engine.memory.session.session_id != session_id


async def test_shutdown_waits_for_background_cleanup(engine):
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def work():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    task = engine.start_background_task(work())
    engine._arm_timeout(60)
    await entered.wait()
    await engine.stop()
    assert task.done()
    assert cleaned.is_set()
    assert not engine._tasks
    assert engine._timeout_task is None


async def test_idle_ticks_wait_for_failed_archive_retry(engine, monkeypatch):
    from muika.core.constants import (
        AUTO_SUMMARY_INTERVAL,
        AUTO_SUMMARY_MIN_TURNS,
        SESSION_IDLE_TIMEOUT,
    )
    from muika.core.events import SessionEndEvent

    clock = [1000.0]
    monkeypatch.setattr("muika.core.loop.time.monotonic", lambda: clock[0])
    engine.state.last_interaction = datetime.now() - timedelta(seconds=SESSION_IDLE_TIMEOUT + 1)
    engine._last_digest_time = datetime.now().timestamp()
    for index in range(AUTO_SUMMARY_MIN_TURNS):
        engine.memory.add_context("user", str(index))
    engine.agent.summarize_session = AsyncMock(side_effect=RuntimeError("offline"))
    engine.memory.update_archive = AsyncMock()
    await engine._process_event(SessionEndEvent(), 0)
    for _ in range(3):
        await engine._tick_idle(TimeTickEvent(), 0)
    assert engine.event_queue.empty()
    assert not engine._tasks
    engine.agent.summarize_session.assert_awaited_once()
    clock[0] += AUTO_SUMMARY_INTERVAL
    await engine._tick_idle(TimeTickEvent(), 0)
    event = engine.event_queue.get_nowait()
    assert event.type == "session_end"
    await engine._process_event(event, 0)
    assert engine.agent.summarize_session.await_count == 2
    assert engine.memory.recent_turns


async def test_concurrent_archive_paths_share_failure_backoff(engine):
    engine.memory.add_context("user", "A memory to keep.")
    engine.agent.summarize_session = AsyncMock(side_effect=RuntimeError("offline"))
    engine.memory.update_archive = AsyncMock()
    results = await asyncio.gather(engine.update_session_memory(), engine.update_session_memory())
    assert results == [False, False]
    engine.agent.summarize_session.assert_awaited_once()
    engine.memory.update_archive.assert_not_awaited()
