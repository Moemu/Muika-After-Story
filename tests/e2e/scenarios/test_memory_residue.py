"""跨会话情绪残留：冲突时写下的 <memory> 笔记必须在新会话第一句前被注入。"""

from datetime import datetime, timedelta

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_relationship_note_survives_session_end(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="第一次见面。"),
            ScriptedTurn(text="<memory>他说好陪我，却两周没有出现。我还在意这件事。</memory>……嗯，我知道了。"),
            ScriptedTurn(text="……你来了。那件事我还没忘。"),
        ]
    )
    app.scripted.add_route(when=lambda req: req.purpose == "dialogue_summary", text="我们聊了那件事。", name="summary")
    await app.start()
    await app.bootstrap()
    assert await app.next_reply() == "第一次见面。"

    await app.user_says("我昨天说好陪你的，结果忘了。")
    assert await app.next_reply() == "……嗯，我知道了。"
    await app.wait_processed("user_message")

    notes = await app.muika.memory.recent_notes()
    assert notes and "两周" in notes[0]

    await app.end_session()
    await app.wait_processed("session_end")

    await app.bootstrap(last_chat_time=datetime.now() - timedelta(days=1))
    await app.wait_processed("session_bootstrap")
    call = next(call for call in reversed(app.scripted.calls) if "A new session has begun" in call.get("prompt", ""))
    # 新会话的第一句必须能看见她写下的关系笔记（跨 session 残留）
    assert "Notes you wrote to your future self" in call["prompt"]
    assert "两周" in call["prompt"]
    app.recorder.record("note_survives_session_end", notes=len(notes))
