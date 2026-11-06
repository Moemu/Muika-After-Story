"""内心连续性：完整原始输出（含 <heart>）必须进入下一轮上下文。"""

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_heart_content_is_returned_to_context(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="<heart>我想好了一个数字：42。先不告诉他。</heart>好，我心里已经有一个数字了，你猜？"),
            ScriptedTurn(text="……是 42 吧？你怎么一下就猜到了。"),
        ]
    )
    await app.start()
    await app.user_says("我猜数字，你心里想一个。")
    assert await app.next_reply() == "好，我心里已经有一个数字了，你猜？"
    await app.wait_processed("user_message")

    # 原始输出（含 heart）必须进入对话上下文，而不是只留可见文本
    last_turn = app.muika.memory.recent_turns[-1]
    assert last_turn.role == "muika"
    assert "<heart>" in last_turn.content
    assert "42" in last_turn.content

    await app.user_says("我猜是 42。")
    assert await app.next_reply() == "……是 42 吧？你怎么一下就猜到了。"
    app.recorder.record("heart_context_continuity", heart_in_context=True)


async def test_heart_does_not_leak_to_visible(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(text="<heart>秘密想法</heart>嗯，我在听。")])
    await app.start()
    await app.user_says("在吗？")
    assert await app.next_reply() == "嗯，我在听。"
    assert all("<heart>" not in sent for sent in app.sent)
    assert all("秘密想法" not in sent for sent in app.sent)
    app.recorder.record("heart_private_visible", leaked=0)
