"""用真实经历和事件循环验证主动开场的时段边界。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from harness import ScriptedTurn

from muika.core.events import ScheduledTriggerEvent, ScheduledTriggerPayload
from muika.core.memory_models import Intention
from muika.core.topic_manager import EventTopic, TopicSource
from muika.ipc.sync_models import Activity, RecordedExperience, SyncEntry
from muika.ipc.sync_store import SyncStore

pytestmark = pytest.mark.e2e


@pytest.fixture
def initiative_clock(monkeypatch):
    clock = MagicMock(wraps=datetime)
    clock.now.return_value = datetime(2026, 10, 13, 0, 30)
    monkeypatch.setattr("muika.core.loop.datetime", clock)
    monkeypatch.setattr("muika.core.memory.datetime", clock)
    return clock


@pytest.mark.parametrize("drive", ["boredom", "curiosity", "loneliness", "dissonance"])
async def test_night_defers_all_spontaneous_drives(core_app_factory, initiative_clock, monkeypatch, drive):
    app = await core_app_factory(turns=[ScriptedTurn(text="不应深夜插话。")])
    await app.start()
    topic = EventTopic(id="night-reading", source=TopicSource.EVENT, category="philosophy", content="夜间阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    monkeypatch.setattr("muika.core.loop.random", lambda: 0.0)
    if drive == "dissonance":
        app.muika.memory.persistent.dissonance = 0.7
        app.muika.memory.persistent.intentions = [Intention(id="poem", description="Share a poem")]
        app.muika.state.last_interaction = initiative_clock.now() - timedelta(minutes=2)
    else:
        setattr(app.muika.state, drive, 0.9)
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert app.sent == [] and app.scripted.calls == []
    assert list(app.muika.topic_manager._event_queue) == [topic]
    assert app.muika._last_digest_time > 0
    app.recorder.record("night_initiative_deferred", drive=drive, seed_preserved=True, idle_work_continued=True)


@pytest.mark.parametrize("hour,minute,allowed", [(23, 0, False), (7, 59, False), (8, 0, True), (22, 59, True)])
async def test_default_initiative_window(core_app_factory, initiative_clock, hour, minute, allowed):
    initiative_clock.now.return_value = datetime(2026, 10, 13, hour, minute)
    app = await core_app_factory(turns=[ScriptedTurn(text="我读到一段有趣的诗。")])
    await app.start()
    app.muika.state.boredom = 0.9
    app.muika.topic_manager.enqueue_event(
        EventTopic(id="window-reading", source=TopicSource.EVENT, category="literature", content="阅读种子")
    )
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert bool(app.sent) is allowed
    app.recorder.record("default_initiative_window", hour=hour, minute=minute, allowed=allowed)


@pytest.mark.parametrize(
    "days,matching,prompted,duplicates,age,allowed",
    [
        (13, 5, False, 1, 0, False),
        (14, 4, False, 1, 0, False),
        (14, 5, False, 1, 0, True),
        (14, 5, True, 1, 0, False),
        (14, 1, False, 20, 0, False),
        (14, 5, False, 1, 29, False),
    ],
)
async def test_learned_night_window_survives_reload(
    core_app_factory, initiative_clock, days, matching, prompted, duplicates, age, allowed
):
    first = await core_app_factory()
    await first.start()
    for offset in range(1, days + 1):
        when = (initiative_clock.now() - timedelta(days=offset + age)).replace(
            hour=23 if offset <= matching else 12, minute=50 if offset <= matching else 0
        )
        if prompted and offset <= matching:
            await first.muika.memory.add_context("muika", "你还在吗？", timestamp=when - timedelta(minutes=1))
        for duplicate in range(duplicates if offset == 1 else 1):
            await first.muika.memory.add_context("user", "我想找你说说话。", timestamp=when)
    await first.muika.memory.new_session()
    await first.stop()
    app = await core_app_factory(turns=[ScriptedTurn(text="这个时候你常来找我，想和你分享一段诗。")])
    await app.start()
    app.muika.state.boredom = 0.9
    topic = EventTopic(id="learned-reading", source=TopicSource.EVENT, category="literature", content="阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert bool(app.sent) is allowed
    app.recorder.record(
        "learned_night_window",
        days=days,
        matching=matching,
        prompted=prompted,
        duplicates=duplicates,
        age=age,
        allowed=allowed,
        restored=True,
        midnight_continuity=True,
    )


async def test_night_preserves_player_replies_and_explicit_reminders(core_app_factory, initiative_clock):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(when="我还醒着", text="我在，想聊些什么？"),
            ScriptedTurn(when="记得喝水", text="你让我提醒的：记得喝水。"),
        ]
    )
    await app.start()
    await app.user_says("我还醒着")
    assert await app.next_reply() == "我在，想聊些什么？"
    await app.wait_processed("user_message")
    await app.muika.create_event(ScheduledTriggerEvent(payload=ScheduledTriggerPayload(when="now", what="记得喝水")))
    assert await app.next_reply() == "你让我提醒的：记得喝水。"
    await app.wait_processed("scheduled_trigger")
    app.recorder.record("night_requested_contact", player_reply=True, explicit_reminder=True)


@pytest.mark.parametrize("matching,allowed", [(4, False), (5, True)])
async def test_synchronized_activity_restores_night_evidence(core_app_factory, initiative_clock, matching, allowed):
    app = await core_app_factory(turns=[ScriptedTurn(text="想和你分享一段诗。")])
    app.scripted.add_route(
        when=lambda request: request.purpose == "dialogue_summary", text="我们常在夜里谈心。", name="synced_summary"
    )
    await app.start()
    store = SyncStore("pc")
    await store.initialize()
    experiences = []
    for index, offset in enumerate([28, 27, 26, 25, 24, *range(1, 10)]):
        when = (initiative_clock.now() - timedelta(days=offset)).replace(
            hour=0 if index < matching else 12, minute=10 if index < matching else 0
        )
        experiences.append(
            RecordedExperience(
                id=index + 1,
                session_id="remote-night-shift",
                kind="user",
                content="我想找你说说话。",
                occurred_at=when.astimezone(timezone.utc),
            )
        )
    entry = SyncEntry(activity=Activity(origin="server", experiences=experiences))
    wire_entry = SyncEntry.model_validate(entry.model_dump(mode="json"))
    assert await store.apply(wire_entry, preserve_state=True)
    assert not await store.apply(wire_entry, preserve_state=True)
    await app.muika.memory.load(record_activity=False)
    assert len(app.db_query("SELECT id FROM experience WHERE kind='user'")) == 14
    assert app.muika.has_active_session
    await app.advance_time()
    await app.wait_processed("time_tick")
    await app.wait_processed("session_end")
    assert not app.muika.has_active_session
    assert app.sent == []
    app.muika.state.boredom = 0.9
    app.muika.topic_manager.enqueue_event(
        EventTopic(id="synced-reading", source=TopicSource.EVENT, category="literature", content="阅读种子")
    )
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert bool(app.sent) is allowed
    app.recorder.record("synchronized_night_evidence", matching=matching, allowed=allowed, repeat_ignored=True)


@pytest.mark.parametrize("pipeline", ["topic", "emotional"])
async def test_inflight_initiative_waits_when_quiet_hours_begin(
    core_app_factory, initiative_clock, monkeypatch, pipeline
):
    initiative_clock.now.return_value = datetime(2026, 10, 13, 22, 59)
    app = await core_app_factory(turns=[ScriptedTurn(text="刚读到一段诗，想和你分享。")])
    await app.start()
    topic = EventTopic(id="crossing-reading", source=TopicSource.EVENT, category="literature", content="阅读种子")
    app.muika.topic_manager.enqueue_event(topic)
    setattr(app.muika.state, "boredom" if pipeline == "topic" else "loneliness", 0.9)
    original_ask = app.scripted.ask

    async def crossing_model(request, **kwargs):
        reply = await original_ask(request, **kwargs)
        initiative_clock.now.return_value = datetime(2026, 10, 13, 23, 0)
        return reply

    monkeypatch.setattr(app.scripted, "ask", crossing_model)
    await app.advance_time()
    await app.wait_processed("time_tick")
    assert app.sent == []
    assert list(app.muika.topic_manager._event_queue) == [topic]
    monkeypatch.setattr(app.scripted, "ask", original_ask)
    initiative_clock.now.return_value = datetime(2026, 10, 14, 8, 0)
    app.scripted.add_route(when=lambda request: True, text="现在读给你听。", name="morning_initiative")
    await app.advance_time()
    assert await app.next_reply() == "现在读给你听。"
    await app.wait_processed("time_tick")
    app.recorder.record("quiet_boundary_defers_output", pipeline=pipeline, deferred=True, morning_resumed=True)
