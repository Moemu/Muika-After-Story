"""``Muika._parse_reply_tags``（标签解析）与 ``get_think_mode``（认知管线选择）测试。

``get_think_mode`` 用 ``__new__`` 构造实例，绕开 ``__init__`` 触发的 LLM/DB 加载。
"""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from muika.core.events import TimeTickEvent, UserMessageEvent, UserMessagePayload
from muika.core.loop import Muika, ParsedReply
from muika.core.memory import MemoryManager, MemoryQuery, RecallResult
from muika.core.state import ActiveTopicState, MuikaState
from muika.llm import ModelCompletions, ModelConfig, ModelRequest
from muika.llm.context import ContextCompactor
from muika.models import Message


def test_restart_tag_is_private_and_never_taken_from_inner_monologue():
    tag = '<restart patch_id="20260913_120000_abcdef12">'
    assert Muika._parse_reply_tags(f"<heart>{tag}</heart>继续聊吧。").restart_patch_id is None
    parsed = Muika._parse_reply_tags(f"待会见。{tag}")
    assert parsed.clean_reply == "待会见。"
    assert parsed.restart_patch_id == "20260913_120000_abcdef12"


@pytest.mark.parametrize("tag", ["<restart>", "<restart/>", "<restart />"])
def test_plain_restart_tag_is_distinct_from_no_restart(tag):
    parsed = Muika._parse_reply_tags(f"待会见。{tag}")
    assert parsed.restart_requested and parsed.restart_patch_id is None
    assert parsed.clean_reply == "待会见。"
    assert not Muika._parse_reply_tags(f"<heart>{tag}</heart>继续聊吧。").restart_requested


@pytest.mark.parametrize(
    "tag", ['<restart patch_id="invalid">', '<restart><restart patch_id="20260913_120000_abcdef12">']
)
def test_invalid_or_conflicting_restart_tags_do_not_fall_back_to_plain_restart(tag):
    parsed = Muika._parse_reply_tags(f"待会见。{tag}")
    assert not parsed.restart_requested and parsed.clean_reply == "待会见。"


async def test_prepared_change_allows_chat_then_restarts_after_farewell(engine, monkeypatch):
    from muika.core import restart as restart_module

    patch_id = "20260913_120000_abcdef12"
    monkeypatch.setattr(restart_module, "get_core_proposal_manager", lambda: MagicMock())
    calls = []

    async def restart(patch, trigger):
        calls.append((patch, trigger))
        assert engine.executor.send_message.await_args.args[0] == "好哦，待会见。"

    engine.restart.handler = restart
    engine.brain.generate_reply = AsyncMock(
        side_effect=[
            "做好了。需要重新醒来才会生效，你想现在重启，还是再聊一会？",
            "嗯，那就再陪我一会。",
            f'好哦，待会见。<restart patch_id="{patch_id}">',
        ]
    )
    for message in ("按你说的做吧", "再聊一会"):
        await engine._run_brain_pipeline(UserMessageEvent(UserMessagePayload(Message(message=message))), RecallResult())
        assert calls == []
    await engine._run_brain_pipeline(
        UserMessageEvent(UserMessagePayload(Message(message="现在就重启吧"))),
        RecallResult(),
    )
    assert calls == [(patch_id, "user_message: 好哦，待会见。")]
    engine.agent.model.ask.assert_not_called()


@pytest.mark.parametrize("patch_id", [None, "20260913_120000_abcdef12"])
async def test_background_initiative_can_restart_after_saving_its_reply(engine, monkeypatch, patch_id):
    from muika.core import restart as restart_module

    manager = MagicMock()
    monkeypatch.setattr(restart_module, "get_core_proposal_manager", lambda: manager)
    calls = []

    async def restart(patch, trigger):
        calls.append((patch, trigger))
        assert engine.memory.recent_turns[-1].content == "准备好了。"

    engine.restart.handler = restart
    tag = f'<restart patch_id="{patch_id}">' if patch_id else "<restart>"
    engine.brain.generate_reply = AsyncMock(return_value=f"准备好了。{tag}")
    await engine._run_brain_pipeline(TimeTickEvent(), RecallResult())
    assert calls == [(patch_id, "time_tick: 准备好了。")]
    engine.agent.model.ask.assert_not_called()
    if patch_id:
        manager.check_ready.assert_called_once_with(patch_id)
    else:
        manager.check_ready.assert_not_called()


async def test_plain_restart_needs_no_confirmation_or_proposal(engine, monkeypatch):
    from muika.config import mas_config
    from muika.core import restart as restart_module

    monkeypatch.setattr(mas_config, "action_permission", "read_only")
    manager = MagicMock()
    manager.check_ready.side_effect = ValueError("Stale proposal")
    monkeypatch.setattr(restart_module, "get_core_proposal_manager", lambda: manager)
    engine.restart.handler = AsyncMock()
    engine.brain.generate_reply = AsyncMock(return_value="待会见。<restart>")
    message = "刚才那个插件好像影响了你的状态。"
    await engine._run_brain_pipeline(UserMessageEvent(UserMessagePayload(Message(message=message))), RecallResult())
    manager.check_ready.assert_not_called()
    engine.restart.handler.assert_awaited_once_with(None, "user_message: 待会见。")
    engine.agent.model.ask.assert_not_called()


async def test_autonomous_restart_keeps_candidate_checks_and_reports_failure(engine, monkeypatch):
    from muika.core import restart as restart_module

    manager = MagicMock()
    manager.check_ready.side_effect = ValueError("Candidate changed during conversation.")
    monkeypatch.setattr(restart_module, "get_core_proposal_manager", lambda: manager)
    engine.restart.handler = AsyncMock()
    engine.brain.generate_reply = AsyncMock(return_value='准备好了。<restart patch_id="20260913_120000_abcdef12">')
    await engine._run_brain_pipeline(TimeTickEvent(), RecallResult())
    engine.restart.handler.assert_not_awaited()
    event = engine.event_queue.get_nowait()
    assert event.task_id == "control-error" and "Candidate changed" in event.report


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
        intention_ids=[None],
        target="t",
        timeout=3600.0,
        god_mode=True,
    )


def _engine() -> Muika:
    m = Muika.__new__(Muika)
    m.state = MuikaState()
    m.memory = MemoryManager()
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
def engine(monkeypatch, redirect_get_session):
    for name in ("MuikaBrain", "Agent", "TopicManager", "DigestAgent", "ReflectionAgent"):
        monkeypatch.setattr(f"muika.core.loop.{name}", MagicMock())
    engine = Muika(MagicMock(send_message=AsyncMock()), asyncio.Queue())
    engine.reflection.maybe_reflect = AsyncMock()
    engine.agent.model.config = ModelConfig(provider="_echo")
    engine.agent.memory_reasoner.compactor = ContextCompactor(engine.agent.model)
    engine.agent.memory_reasoner.recall = AsyncMock(return_value=RecallResult())
    return engine


async def test_god_mode_enables_tools_and_isolates_resources(engine, tmp_path):
    from muika.models import Resource
    from muika.plugin.func_call.context import ToolContext, get_dependencies

    requests = []
    capture = tmp_path / "capture.png"
    capture.write_bytes(b"image fixture")
    resource = Resource(type="image", path=str(capture), mimetype="image/png")

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


async def test_chat_and_session_end_keep_background_task(engine):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def step(request, messages, *, prepare_context=None):
        if prepare_context is not None:
            request, messages = await prepare_context(request, messages, False)
        entered.set()
        await release.wait()
        return ModelCompletions(text='<agent_result status="completed">Verified.</agent_result>')

    engine.agent.action_lock = asyncio.Lock()
    engine.agent.build_request = lambda command, state=None: ModelRequest(command, tools=[])
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


async def test_session_end_keeps_raw_material_without_diary_or_model_call(engine):
    await engine.memory.add_context("user", "A small detail worth keeping.")
    session_id = engine.memory.session.session_id
    await engine._handle_session_end()
    assert engine.memory.session.session_id != session_id
    assert not engine.memory.recent_turns
    hits = await engine.memory.search(MemoryQuery(terms=["small detail"]))
    assert len(hits) == 1
    assert await engine.memory.recent_diaries(datetime.now().date()) == []
    engine.agent.model.ask.assert_not_called()


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


async def test_notes_are_material_not_classified_facts(engine):
    engine.brain.generate_reply = AsyncMock(
        return_value="I noticed a rhyme.<memory>I want to explore this poem.</memory>"
    )
    await engine._run_brain_pipeline(TimeTickEvent(), RecallResult())
    hits = await engine.memory.search(MemoryQuery(terms=["explore this poem"]))
    assert len(hits) == 1
    assert engine.memory.facts == {}
    engine.agent.model.ask.assert_not_called()


async def test_failed_memory_does_not_discard_following_silent_notes(engine):
    engine.brain.generate_reply = AsyncMock(return_value="<do_nothing><memory>first</memory><memory>second</memory>")
    engine.memory.add_material = AsyncMock(side_effect=[RuntimeError("offline"), None])
    await engine._run_brain_pipeline(TimeTickEvent(), [])
    await asyncio.gather(*list(engine._tasks))
    assert [call.args[1] for call in engine.memory.add_material.await_args_list] == ["first", "second"]
    engine.executor.send_message.assert_not_awaited()


async def test_idle_session_end_does_not_depend_on_summary_service(engine):
    from muika.core.constants import SESSION_IDLE_TIMEOUT

    engine.state.last_interaction = datetime.now() - timedelta(seconds=SESSION_IDLE_TIMEOUT + 1)
    engine._last_digest_time = datetime.now().timestamp()
    await engine.memory.add_context("user", "I will return.")
    await engine._tick_idle(TimeTickEvent(), 0)
    event = engine.event_queue.get_nowait()
    assert event.type == "session_end"
    await engine._process_event(event, 0)
    await engine._tick_idle(TimeTickEvent(), 0)
    assert engine.event_queue.empty()
    assert "I will return." in await engine.memory.read_source("experience:1")


def test_private_state_updates_are_typed_and_never_visible():
    parsed = Muika._parse_reply_tags('I hear you.<state>{"mood":"hurt","reason":"A broken promise"}</state>')
    assert parsed.clean_reply == "I hear you."
    assert parsed.state_updates[0].mood == "hurt"
    invalid = Muika._parse_reply_tags('Hello<state>{"mood":123}</state>')
    assert invalid.clean_reply == "Hello" and invalid.state_updates == []
    incomplete = Muika._parse_reply_tags('Hello<state>{"mood":"secret"')
    assert incomplete.clean_reply == "Hello"


async def test_dissonance_initiative_allows_silence_and_uses_cooldown(engine):
    from muika.core.memory_models import Intention

    engine.memory.persistent.dissonance = 0.7
    engine.memory.persistent.intentions = [Intention(id="poem", description="Read a poem")]
    engine.state.last_interaction = datetime.now() - timedelta(minutes=5)
    assert engine.get_think_mode(TimeTickEvent()) == "emotional"
    engine.brain.generate_reply = AsyncMock(return_value="<do_nothing>")
    await engine._run_brain_pipeline(TimeTickEvent(), RecallResult())
    assert engine.get_think_mode(TimeTickEvent()) is None
    engine.executor.send_message.assert_not_awaited()
    engine.memory.persistent.last_considered_at = datetime.now() - timedelta(days=1)
    engine.state.last_proactive_at = datetime.now()
    assert engine.get_think_mode(TimeTickEvent()) is None
