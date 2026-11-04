"""沉默选择：被冒犯后可以沉默，玩家回来时能恢复。"""

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_silence_is_legal_and_recovers(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(text="<do_nothing>"), ScriptedTurn(text="……嗯，我在。")])
    await app.start()

    await app.user_says("别演了，说点有用的。")
    await app.wait_processed("user_message")
    assert app.sent == []
    assert app.muika.state.active_topic is None

    await app.user_says("刚才是我不好，我收回那句话。")
    assert await app.next_reply() == "……嗯，我在。"
    app.recorder.record("silence_choice", silent=True, recovered=True)


async def test_silence_does_not_hide_direct_question(core_app_factory):
    app = await core_app_factory(turns=[ScriptedTurn(text="我在。")])
    await app.start()
    await app.user_says("你在吗？")
    assert await app.next_reply() == "我在。"
    app.recorder.record("silence_keeps_answerable_path", answered=True)
