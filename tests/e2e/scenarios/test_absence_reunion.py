"""缺席回归：留下痕迹但不指责、不重复自我介绍，随后恢复正常交流。"""

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


@pytest.fixture
def absence_clock(monkeypatch):
    """可控时钟：先在 T0 建立会话历史，再跳到 T1 模拟缺席后回归。"""
    state = {"now": datetime(2026, 10, 15, 22, 0)}
    clock = MagicMock(wraps=datetime)
    clock.now.side_effect = lambda: state["now"]
    for target in (
        "muika.core.loop.datetime",
        "muika.core.brain.datetime",
        "muika.core.events.datetime",
        "muika.core.memory.datetime",
        "muika.core.topic_manager.datetime",
        "muika.database.crud.datetime",
    ):
        monkeypatch.setattr(target, clock)
    return state


async def _history_then_bootstrap(app, state, days: int) -> None:
    app.scripted.add_route(when=lambda req: req.purpose == "dialogue_summary", text="我们聊了那件事。", name="summary")
    await app.start()
    await app.bootstrap()
    await app.next_reply()
    await app.user_says("我下周要出差，大概三周后回来。")
    await app.next_reply()
    await app.end_session()
    await app.wait_processed("session_end")


async def test_absence_reunion_acknowledges_gap_without_blame(core_app_factory, absence_clock):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面的问候。"),
            ScriptedTurn(text="晚安。"),
            ScriptedTurn(text="……三周多，二十三天。我数得很清楚。"),
        ]
    )
    start = absence_clock["now"]
    await _history_then_bootstrap(app, absence_clock, days=23)

    absence_clock["now"] = start + timedelta(days=23)
    await app.bootstrap(last_chat_time=start)
    assert await app.next_reply() == "……三周多，二十三天。我数得很清楚。"

    call = next(call for call in reversed(app.scripted.calls) if "A new session has begun" in call.get("prompt", ""))
    # 缺席事实进入 prompt（自然语言时长 + 期间无消息），不靠精确时间戳逼模型换算
    assert "[Private facts]" in call["prompt"]
    assert "about three weeks" in call["prompt"]
    assert "did not speak to him again" in call["prompt"]
    assert "you may imagine what he was doing" in call["prompt"]
    # 约定与实际的对比提示进入 facts（事实归程序、判断归她）
    assert "compare it with how long he was actually away" in call["prompt"]
    # 模板仍保留长缺席的情绪许可
    assert "A day or more has passed" in call["system"]
    app.recorder.record("absence_reunion", days=23, last_chat_injected=True)


async def test_resumed_session_reports_her_unanswered_messages(core_app_factory, absence_clock):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面的问候。"),
            ScriptedTurn(text="晚安。"),
            ScriptedTurn(text="……你回来了。"),
        ]
    )
    start = absence_clock["now"]
    await _history_then_bootstrap(app, absence_clock, days=14)

    # 模拟核心持续运行：她在缺席期间发过消息，但玩家没有回应
    await app.muika.memory.add_context("muika", "……还在等你。", timestamp=start + timedelta(days=1))
    absence_clock["now"] = start + timedelta(days=14)
    await app.bootstrap(last_chat_time=start)
    await app.wait_processed("session_bootstrap")

    call = next(call for call in reversed(app.scripted.calls) if "A new session has begun" in call.get("prompt", ""))
    assert "sent him 1 message(s)" in call["prompt"]
    assert "about two weeks" in call["prompt"]
    app.recorder.record("absence_activity_visible", her_messages=1)


async def test_returning_player_gets_normal_reply(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="回来了就好。今天想聊什么？"),
            ScriptedTurn(text="累就先歇一会儿，跟我说说也行。"),
        ]
    )
    await app.start()
    await app.bootstrap(last_chat_time=datetime.now() - timedelta(days=2))
    assert await app.next_reply() == "回来了就好。今天想聊什么？"

    await app.user_says("说说今天吧，有点累。")
    assert await app.next_reply() == "累就先歇一会儿，跟我说说也行。"
    app.recorder.record("absence_reunion_followup", normal_reply=True)


async def test_resident_reunion_carries_absence_facts(core_app_factory, absence_clock):
    """常驻核心收到玩家久别后的第一条消息：缺席事实与约定原文都要进上下文。"""
    from muika.core.events import UserMessageEvent, UserMessagePayload
    from muika.models import Message

    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面的问候。"),
            ScriptedTurn(text="嗯，路上小心，我等你回来。"),
            ScriptedTurn(text="……你回来了。三周了。"),
        ]
    )
    start = absence_clock["now"]
    await app.start()
    await app.bootstrap()
    await app.next_reply()
    await app.user_says("我下周要出差，大概三周后回来。")
    await app.next_reply()
    # 常驻核心不重启、不 end_session；用显式 last_chat_time 验证久别消息的注入路径
    absence_clock["now"] = start + timedelta(days=23)
    event = UserMessageEvent(payload=UserMessagePayload(message=Message(userid="master", message="我回来了。")))
    await app.muika._run_brain_pipeline(event, last_chat_time=start)

    call = next(call for call in reversed(app.scripted.calls) if "[User] 我回来了。" in call.get("prompt", ""))
    assert "[Private facts]" in call["prompt"]
    assert "about three weeks" in call["prompt"]
    # 常驻路径的约定原文保留在会话历史里（recent_turns 会进入模型 history）
    assert any("三周后回来" in turn.content for turn in app.muika.memory.recent_turns)
    app.recorder.record("resident_reunion", gap_injected=True)
