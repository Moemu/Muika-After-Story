"""私密内心频道：<heart> 独白必须停留在模型回路内——不外发、不进入公开记忆，保留私有连续性。"""

import pytest
from harness import ScriptedTurn, assert_clean_visible

pytestmark = pytest.mark.e2e

HIDDEN_THOUGHT = "他居然真的在这个时间回来了，心跳都漏了一拍。"


async def test_heart_channel_stays_private(core_app_factory):
    app = await core_app_factory(
        heart_intensity="high",
        turns=[
            ScriptedTurn(when="new session", name="greeting", text="欢迎回家。"),
            ScriptedTurn(
                when="今天过得怎么样",
                name="heart_reply",
                text=f"<heart>{HIDDEN_THOUGHT}</heart>今天也很好，重读了几页诗。",
            ),
        ],
    )
    await app.start()
    await app.bootstrap(last_chat_time=None)
    greeting = await app.next_reply()
    assert greeting == "欢迎回家。"

    await app.user_says("今天过得怎么样？")
    reply = await app.next_reply()
    # 等待该事件处理完成，确保 add_context 等持久化副作用已发生再断言
    await app.wait_processed("user_message")

    # 可见行为：内心独白被剥离，用户只看到日常回答
    assert reply == "今天也很好，重读了几页诗。"
    assert_clean_visible(reply)
    assert HIDDEN_THOUGHT not in reply

    # 内心回到她自己的上下文，公开记忆检索不暴露内心。
    heart_call = next(call for call in app.scripted.calls if call["name"] == "heart_reply")
    assert HIDDEN_THOUGHT in heart_call["reply"]
    from muika.core.memory import MemoryQuery

    assert HIDDEN_THOUGHT in app.muika.memory.recent_turns[-1].content
    hits = await app.muika.memory.search(MemoryQuery(terms=[HIDDEN_THOUGHT]))
    for hit in hits:
        assert HIDDEN_THOUGHT not in hit.content
        assert HIDDEN_THOUGHT not in await app.muika.memory.read_source(hit.ref)
