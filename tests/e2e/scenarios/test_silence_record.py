"""沉默记录：主动选择不回复时留下可复算记录，直接提问仍有回答通路。"""

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_silence_is_recorded(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(text="<do_nothing>")])
    await app.start()
    await app.user_says("随便说点什么吧。")
    await app.wait_processed("user_message")

    assert app.sent == []
    assert await app.muika.memory.silence_count() == 1
    # 沉默的原始输出（含 <do_nothing>）必须回到上下文，下一轮才知道她为何沉默
    last_turn = app.muika.memory.recent_turns[-1]
    assert last_turn.role == "muika" and "<do_nothing>" in last_turn.content
    app.recorder.record("silence_recorded", count=1)


async def test_silence_does_not_pollute_notes(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(text="<do_nothing>")])
    await app.start()
    await app.user_says("随便说点什么吧。")
    await app.wait_processed("user_message")

    # 沉默记录不能被当作她写给未来的关系笔记注入
    assert await app.muika.memory.recent_notes() == []
    app.recorder.record("silence_not_in_notes", clean=True)
