"""会话生命周期：初见、对话、会话结束，以及核心重启后带着缺席记忆的回归。"""

from datetime import datetime, timedelta

import pytest
from harness import ScriptedTurn, assert_clean_visible

pytestmark = pytest.mark.e2e


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
