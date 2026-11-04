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
    # 缺席事实（上次发言时间）与长缺席语义应进入 system，而不是靠模型猜
    assert last_chat.strftime("%Y-%m-%d %H:%M:%S") in call["system"]
    assert "A day or more has passed" in call["system"]
    app.recorder.record("absence_reunion", days=23, last_chat_injected=True)


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
