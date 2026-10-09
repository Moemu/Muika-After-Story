"""会话生命周期：初见、对话、会话结束，以及核心重启后带着缺席记忆的回归。"""

import asyncio
from datetime import datetime, timedelta

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.database.db import get_session

pytestmark = pytest.mark.e2e


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
