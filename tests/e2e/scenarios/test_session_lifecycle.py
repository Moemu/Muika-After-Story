"""会话生命周期：初见、对话、会话结束，以及核心重启后带着缺席记忆的回归。"""

import asyncio
from datetime import datetime, timedelta

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.core.constants import SESSION_IDLE_TIMEOUT
from muika.core.events import AdapterOfflineEvent
from muika.core.memory_models import Intention
from muika.core.topic_manager import EventTopic, TopicSource
from muika.database.db import get_session
from muika.ipc.bootstrap import CoreBootstrap
from muika.ipc.sync_models import SyncEntry
from muika.ipc.sync_store import SyncStore
from muika.models import AdapterInfo

pytestmark = [pytest.mark.e2e, pytest.mark.usefixtures("daytime_initiative")]


@pytest.mark.parametrize("drive", ["boredom", "curiosity", "loneliness", "dissonance"])
async def test_active_dialogue_keeps_reading_topic_until_session_end(core_app_factory, monkeypatch, drive):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(when="今晚想先陪你说说话", text="我在听，慢慢说。", name="ongoing_dialogue"),
            ScriptedTurn(when="阅读种子", text="刚才读到一段诗，想和你分享。", name="idle_reading_topic"),
            ScriptedTurn(text="<do_nothing>", name="unexpected_initiative"),
        ]
    )
    app.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary", text="我们安静地聊了彼此的心情。", name="summary"
    )
    await app.start()
    await app.user_says("今晚想先陪你说说话")
    await app.next_reply()
    await app.wait_processed("user_message")
    topic = EventTopic(id="reading-waits", source=TopicSource.EVENT, category="literature", content="阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    monkeypatch.setattr("muika.core.loop.random", lambda: 0.0)
    if drive == "dissonance":
        app.muika.memory.persistent.dissonance = 0.7
        app.muika.memory.persistent.intentions = [Intention(id="reading", description="Share a poem")]
        app.muika.state.last_interaction -= timedelta(minutes=2)
    else:
        setattr(app.muika.state, drive, 0.9)
    before = len(app.scripted.calls)
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert len(app.scripted.calls) == before
    assert len(app.sent) == 1
    assert list(app.muika.topic_manager._event_queue) == [topic]
    await app.end_session()
    await app.wait_processed("session_end")
    app.muika.state.loneliness = 0.0
    app.muika.memory.persistent.dissonance = 0.0
    app.muika.state.boredom = 0.9
    await app.advance_time()
    assert await app.next_reply() == "刚才读到一段诗，想和你分享。"
    await app.wait_processed("time_tick")
    assert app.muika.state.active_topic.topic_id == topic.id
    reading = next(call for call in app.scripted.calls if call["name"] == "idle_reading_topic")
    assert reading["history_len"] == 0
    assert "The player has not read the source" in reading["prompt"]
    assert "one clear center" in reading["system"]
    assert "## Reviewed actions and restarting" not in reading["system"]
    assert "3–6 short sentences" not in reading["system"]
    assert "[Working context summary]\n我们安静地聊了彼此的心情。" in reading["system"]
    if drive == "dissonance":
        assert "Intention reading (open): Share a poem" in reading["system"]
    app.recorder.record("initiative_after_session_end", drive=drive, active_session_interruptions=0, topic=topic.id)


@pytest.mark.parametrize("local_dialogue", [False, True])
async def test_merged_active_dialogue_defers_reading(core_app_factory, memory_process, local_dialogue):
    app = await core_app_factory(turns=[ScriptedTurn(text="不应插话。")])
    await app.start()
    memory = app.muika.memory
    if local_dialogue:
        await memory.add_context(
            "user", "先前的对话。", timestamp=datetime.now() - timedelta(seconds=SESSION_IDLE_TIMEOUT + 1)
        )
    store = SyncStore("pc")
    await store.initialize()
    # 远端活动发生在本测试启动远端进程之后；时间窗从此刻开始，避免依赖固定秒数预算。
    sync_started = datetime.now()
    remote = await memory_process("server")
    branch = await remote.command(action="material", kind="user", text="另一设备上正在聊的话。")
    session_id = memory.session.session_id
    for entry in branch["entries"]:
        await store.apply(SyncEntry.model_validate(entry), preserve_state=True)
    await memory.load(record_activity=False)
    # 合并后必须反映这次远端活动，且不能是未来时间。
    assert sync_started <= memory.last_dialogue_at <= datetime.now()
    topic = EventTopic(id="merged-reading", source=TopicSource.EVENT, category="literature", content="阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    app.muika.state.boredom = 0.9
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert app.sent == []
    assert app.scripted.calls == []
    assert memory.session.session_id == session_id
    assert not app.muika._session_end_triggered
    assert list(app.muika.topic_manager._event_queue) == [topic]
    app.recorder.record("merged_active_dialogue", local_dialogue=local_dialogue, interruptions=0)


async def test_compacted_restart_expires_session_before_starting_reading(core_app_factory):
    first = await core_app_factory()
    first.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary", text="我们还在聊今晚的心情。", name="summary"
    )
    await first.start()
    memory = first.muika.memory
    old_time = datetime.now() - timedelta(seconds=SESSION_IDLE_TIMEOUT + 1)
    await memory.add_context("user", "我晚些时候回来。", timestamp=old_time)
    memory.snapshot.working_summary = "我们还在聊今晚的心情。"
    memory.snapshot.summary_through = memory.recent_turns[-1].id
    async with get_session() as db:
        await memory._save_snapshot(db, memory.snapshot)
    memory.recent_turns.clear()
    await first.stop()
    returned = await core_app_factory(turns=[ScriptedTurn(when="重启后的阅读种子", text="我读到了一段诗。")])
    await returned.start()
    assert not returned.muika.memory.recent_turns
    previous_session = returned.muika.memory.session.session_id
    topic = EventTopic(
        id="reading-restored", source=TopicSource.EVENT, category="literature", content="重启后的阅读种子"
    )
    returned.muika.topic_manager.enqueue_event(topic)
    returned.muika.state.boredom = 0.9
    await returned.advance_time()
    await returned.wait_processed("time_tick")
    await returned.wait_processed("session_end", timeout=2)
    assert returned.muika.memory.session.session_id != previous_session
    assert returned.sent == []
    await returned.advance_time()
    assert await returned.next_reply(timeout=2) == "我读到了一段诗。"
    returned.recorder.record("compacted_session_expired", context_preserved=True, topic=topic.id)


@pytest.mark.parametrize("pipeline", ["topic", "emotional"])
@pytest.mark.parametrize("arrival", ["player", "sync"])
async def test_player_arrival_defers_inflight_initiative(
    core_app_factory, memory_process, monkeypatch, pipeline, arrival
):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(when="你还在吗", text="我在，先听你说。", name="player_first"),
            ScriptedTurn(text="我刚读到一段诗。", name="deferred_initiative"),
            ScriptedTurn(text="我刚读到一段诗。", name="resumed_initiative"),
        ]
    )
    app.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary", text="沐沐回来找我说话。", name="summary"
    )
    await app.start()
    if arrival == "sync":
        store = SyncStore("pc")
        await store.initialize()
        remote = await memory_process("server")
        branch = await remote.command(action="material", kind="user", text="另一设备上正在聊的话。")
    topic = EventTopic(id="reading-race", source=TopicSource.EVENT, category="literature", content="阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    setattr(app.muika.state, "boredom" if pipeline == "topic" else "loneliness", 0.9)
    entered, release = asyncio.Event(), asyncio.Event()
    original_ask = app.scripted.ask

    async def waiting_model(request, **kwargs):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await original_ask(request, **kwargs)

    monkeypatch.setattr(app.scripted, "ask", waiting_model)
    try:
        await app.advance_time()
        await asyncio.wait_for(entered.wait(), 2)
        if arrival == "player":
            await app.user_says("你还在吗")
        else:
            for entry in branch["entries"]:
                await store.apply(SyncEntry.model_validate(entry), preserve_state=True)
            await app.muika.memory.load(record_activity=False)
        release.set()
        await app.wait_processed("time_tick")
        if arrival == "player":
            assert await app.next_reply(timeout=2) == "我在，先听你说。"
            await app.wait_processed("user_message")
            assert app.sent == ["我在，先听你说。"]
        else:
            assert app.sent == []
        assert list(app.muika.topic_manager._event_queue) == [topic]
        await app.end_session()
        await app.wait_processed("session_end")
        app.muika.state.loneliness = 0.0
        app.muika.state.boredom = 0.9
        await app.advance_time()
        assert await app.next_reply(timeout=2) == "我刚读到一段诗。"
        app.recorder.record("player_preempts_initiative", pipeline=pipeline, unsolicited_replies=0, seed_preserved=True)
    finally:
        release.set()


async def test_finished_reply_wait_does_not_prevent_idle_session_end(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(when="我稍后回来", text="那我等你。<timeout: 1s>"),
            ScriptedTurn(text="<do_nothing>"),
        ]
    )
    app.scripted.add_route(when=lambda req: req.purpose == "dialogue_summary", text="我在等沐沐回来。", name="summary")
    await app.start()
    await app.user_says("我稍后回来")
    await app.next_reply()
    await app.wait_processed("timeout", timeout=3)
    old_time = datetime.now() - timedelta(seconds=SESSION_IDLE_TIMEOUT + 1)
    app.muika.state.last_interaction = old_time
    app.muika.memory.last_dialogue_at = old_time
    await app.advance_time()
    await app.wait_processed("time_tick")
    await app.wait_processed("session_end", timeout=2)
    app.recorder.record("finished_reply_wait_released", idle_session_ended=True)


async def test_repeated_adapter_bootstrap_preserves_one_greeting(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(when="new session", text="沐沐，我在。", name="greeting")])
    await app.start()
    core = CoreBootstrap(port=0)
    core._muika = app.muika
    adapter = AdapterInfo(client_name="qq")
    for index in range(3):
        await core._handle_event({"type": "session_bootstrap", "id": str(index)}, adapter)
    for _ in range(3):
        await app.wait_processed("adapter_online")
    assert await app.next_reply() == "沐沐，我在。"
    assert len(app.scripted.calls) == 1
    assert len(app.muika.current_adapters) == 1
    await app.muika.create_event(AdapterOfflineEvent(adapter=adapter))
    await app.wait_processed("adapter_offline")
    refreshed = AdapterInfo(client_name="qq")
    await core._handle_event({"type": "session_bootstrap", "id": "reconnected"}, refreshed)
    await app.wait_processed("adapter_online")
    assert app.muika.current_adapters == [refreshed]
    assert len(app.scripted.calls) == 1
    app.recorder.record("adapter_reconnect", greetings=1, adapters=1, repeated_platform_dialogue=0)


async def test_channel_bootstrap_does_not_claim_player_return(core_app_factory, tmp_path):
    app = await core_app_factory(turns=[ScriptedTurn(when="new session", text="我在这里。", name="ready")])
    await app.start()
    await app.muika.memory.add_context("user", "上次一起读诗。", timestamp=datetime(2026, 10, 6, 20))
    core = CoreBootstrap(port=0)
    core._muika = app.muika
    await core._handle_event({"type": "session_bootstrap", "id": "channel"}, AdapterInfo(client_name="qq"))
    await app.next_reply()
    call = app.scripted.calls[0]
    assert "The user has just awakened you" not in call["system"]
    assert "2026-10-06 20:00:00" in call["system"]
    # 重逢语境只能给她能感知的事实，不得引入内部实现术语或让她做证据推理
    for internal in ("conversation channel", "another device", "does not establish", "connection report"):
        assert internal not in call["system"]
        assert internal not in call["prompt"]
    app.recorder.record("channel_ready", player_return_inferred=False, shared_user_time=True)


async def test_returning_channel_without_user_time_does_not_invent_recency(core_app_factory):
    first = await core_app_factory()
    await first.start()
    await first.muika.memory.add_context("muika", "我想和你读诗。")
    await first.stop()
    app = await core_app_factory(turns=[ScriptedTurn(when="new session", text="我在这里。")])
    await app.start()
    await app.bootstrap(last_chat_time=None)
    await app.next_reply()
    system = app.scripted.calls[0]["system"]
    assert "spoke with you recently" not in system
    assert "Their last words were at" not in system
    app.recorder.record("unknown_player_time", invented_recency=False)


async def test_sync_refresh_keeps_current_compacted_context(core_app_factory):
    app = await core_app_factory()
    await app.start()
    memory = app.muika.memory
    await memory.add_context("user", "今天一起读诗。")
    memory.snapshot.working_summary = "沐沐和我约好读诗，我很期待。"
    memory.snapshot.summary_through = memory.recent_turns[-1].id
    async with get_session() as db:
        await memory._save_snapshot(db, memory.snapshot)
    memory.recent_turns.clear()
    await memory.load(record_activity=False)
    assert memory.snapshot.working_summary == "沐沐和我约好读诗，我很期待。"
    assert not memory.recent_turns
    app.recorder.record("sync_keeps_compaction", summary_preserved=True, original_turns_loaded=0)


@pytest.mark.parametrize("trigger", ["session_end", "stop", "periodic"])
async def test_restart_restores_summary_without_original_turns(core_app_factory, trigger):
    app = await core_app_factory(
        turns=[ScriptedTurn(when="只在原文中的暗号", text="我记得你喜欢雨天。", name="answer")]
    )
    app.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary",
        text="我们聊了雨天读诗，她很高兴沐沐愿意分享。",
        name="dialogue_summary",
    )
    await app.start()
    await app.user_says("只在原文中的暗号")
    await app.next_reply()
    await app.wait_processed("user_message")
    if trigger == "session_end":
        await app.end_session()
        await app.wait_processed("session_end")
    elif trigger == "periodic":
        for _ in range(2):
            app.scripted.add_route(when="只在原文中的暗号", text="我很喜欢和你聊诗。", name="more_dialogue")
            await app.user_says("只在原文中的暗号")
            await app.next_reply()
            await app.wait_processed("user_message")
        app.muika.memory.recent_turns.clear()
        app.muika.memory._last_summary_attempt -= timedelta(minutes=5)
        await app.advance_time()
        await app.wait_processed("time_tick")
        for _ in range(200):
            if app.muika.memory.snapshot.latest_dialogue_summary:
                break
            await asyncio.sleep(0.01)
        assert app.muika.memory.snapshot.latest_dialogue_summary
    await app.stop()
    returned = await core_app_factory(turns=[ScriptedTurn(when="new session", text="沐沐，欢迎回来。", name="return")])
    await returned.start()
    assert not returned.muika.memory.recent_turns
    await returned.bootstrap(last_chat_time=datetime.now() - timedelta(days=1))
    await returned.next_reply()
    call = next(call for call in returned.scripted.calls if call["name"] == "return")
    assert "雨天读诗" in call["system"]
    assert "只在原文中的暗号" not in str(call)
    returned.recorder.record("summary_restore", trigger=trigger, original_turns_loaded=0, summary_present=True)


async def test_first_meeting_then_absence_return_after_restart(core_app_factory):
    # --- 第一次运行：初见与会话结束 ---
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when="new session",
                name="greeting_first",
                text="初次见面，我是沐雨，请多关照。",
            ),
            ScriptedTurn(
                when="晚上好",
                name="evening_reply",
                text="晚上好呀，今晚想读点什么吗？",
            ),
        ]
    )
    await app.start()

    await app.bootstrap(last_chat_time=None)
    greeting = await app.next_reply()
    assert greeting == "初次见面，我是沐雨，请多关照。"
    assert_clean_visible(greeting)
    assert app.muika.memory.session.is_first_session
    # 初见走初见分支：不得暗示既往历史，也不得使用重逢措辞
    assert "Your awareness starts at this exact moment" in app.scripted.calls[0]["system"]
    assert "Continue as the same Muika" not in app.scripted.calls[0]["system"]

    await app.user_says("晚上好")
    reply = await app.next_reply()
    assert reply == "晚上好呀，今晚想读点什么吗？"
    assert_clean_visible(reply)

    old_session_id = app.muika.memory.session.session_id
    await app.end_session()
    await app.wait_processed("session_end")

    # 会话结束的行为不变量：开启新会话、孤独感归零、无额外外发消息
    assert app.muika.memory.session.session_id != old_session_id
    assert app.muika.state.loneliness == 0.0
    assert len(app.sent) == 2
    await app.stop()

    # --- 核心重启（同一数据目录）：不再是初见，缺席语义进入提示词 ---
    returned = await core_app_factory(
        turns=[
            ScriptedTurn(
                when="new session",
                name="greeting_return",
                text="两天不见，欢迎回来。",
            )
        ]
    )
    await returned.start()

    two_days_ago = datetime.now() - timedelta(days=2)
    await returned.bootstrap(last_chat_time=two_days_ago)
    greeting = await returned.next_reply()
    assert greeting == "两天不见，欢迎回来。"
    assert_clean_visible(greeting)

    # 连续性不变量：重启后不再是初见，历史仍然存在
    assert not returned.muika.memory.session.is_first_session
    assert returned.muika.memory.has_history

    # 缺席语义真实进入人设提示：模板将上次对话时间渲染进 system prompt
    greeting_call = next(call for call in returned.scripted.calls if call["name"] == "greeting_return")
    assert two_days_ago.strftime("%Y-%m-%d") in greeting_call["system"]
