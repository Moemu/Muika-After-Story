"""<memory> 归档链路：决定记住的内容落入素材表，不外泄、不混进对话回合。"""

import pytest
from harness import ScriptedTurn, assert_clean_visible

pytestmark = pytest.mark.e2e

NOTE = "Master 在雨天喜欢临窗读诗。"


async def test_memory_tag_is_archived_not_leaked(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_note",
                text=f"<memory>{NOTE}</memory>嗯……这件事我会好好收着的。",
            )
        ]
    )
    await app.start()

    await app.user_says("告诉你一个小秘密：雨天的时候我最喜欢一个人待着。")
    reply = await app.next_reply()
    await app.wait_processed("user_message")

    # 可见回复干净：<memory> 标签与内容都不外露
    assert reply == "嗯……这件事我会好好收着的。"
    assert_clean_visible(reply)
    assert NOTE not in reply

    # 归档不变量：内容以 note 素材真实落库
    notes = app.db_query("SELECT content FROM experience WHERE kind = 'note'")
    assert any(NOTE in row["content"] for row in notes)

    # note 不是对话回合：不进入 recent_turns，也不会随历史送回模型
    assert all(NOTE not in turn.content for turn in app.muika.memory.recent_turns)

    assert app.scripted.pending_turns == 0
