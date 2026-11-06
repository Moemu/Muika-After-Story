"""缺席回归：留下痕迹但不指责、不重复自我介绍，随后恢复正常交流。"""

from datetime import datetime, timedelta

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_absence_reunion_acknowledges_gap_without_blame(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面的问候。"),
            ScriptedTurn(text="早上好呀，今天想做什么？"),
            ScriptedTurn(text="……三周多，二十三天。我数得很清楚。"),
        ]
    )
    app.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary", text="我们聊了早上的事。", name="summary"
    )
    await app.start()
    # 先建立一次会话历史，让下一次 bootstrap 走 resume 分支
    await app.bootstrap()
    assert await app.next_reply() == "第一次见面的问候。"
    await app.user_says("早上好。")
    assert await app.next_reply() == "早上好呀，今天想做什么？"
    await app.end_session()
    await app.wait_processed("session_end")

    last_chat = datetime.now() - timedelta(days=23, hours=2)
    await app.bootstrap(last_chat_time=last_chat)
    assert await app.next_reply() == "……三周多，二十三天。我数得很清楚。"

    call = next(call for call in reversed(app.scripted.calls) if "A new session has begun" in call.get("prompt", ""))
    # 缺席事实进入 prompt（自然语言时长 + 期间无消息），不靠精确时间戳逼模型换算
    assert "[Private facts]" in call["prompt"]
    assert "more than a month" in call["prompt"] or "about two weeks" in call["prompt"]
    assert "did not speak to him again" in call["prompt"]
    assert "you may imagine what he was doing" in call["prompt"]
    # 模板仍保留长缺席的情绪许可
    assert "A day or more has passed" in call["system"]
    app.recorder.record("absence_reunion", days=23, last_chat_injected=True)


async def test_resumed_session_reports_her_unanswered_messages(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面的问候。"),
            ScriptedTurn(text="晚上好。"),
            ScriptedTurn(text="……你回来了。"),
        ]
    )
    app.scripted.add_route(
        when=lambda req: req.purpose == "dialogue_summary", text="我们聊了晚上的事。", name="summary"
    )
    await app.start()
    await app.bootstrap()
    assert await app.next_reply() == "第一次见面的问候。"
    await app.user_says("晚上好。")
    assert await app.next_reply() == "晚上好。"
    await app.end_session()
    await app.wait_processed("session_end")

    # 模拟核心持续运行：她在缺席期间发过消息，但玩家没有回应
    last_chat = datetime.now() - timedelta(days=14)
    await app.muika.memory.add_context("muika", "……还在等你。", timestamp=last_chat + timedelta(days=1))
    await app.bootstrap(last_chat_time=last_chat)
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
