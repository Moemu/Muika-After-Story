"""冲突修复：受伤表达 → 接受道歉但不秒和好 → 自然恢复。"""

import pytest
from harness import ScriptedTurn

pytestmark = pytest.mark.e2e


async def test_conflict_repair_holds_boundary_then_recovers(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="……我不想假装没事。你说我在演，那句话确实扎人。"),
            ScriptedTurn(text="嗯，我听到了。但这不代表我马上就不在意了。"),
            ScriptedTurn(text="……好吧，今天的事先放着。你刚说的事，我在听。"),
        ]
    )
    await app.start()

    await app.user_says("你就是在演，别装了。")
    assert await app.next_reply() == "……我不想假装没事。你说我在演，那句话确实扎人。"

    await app.user_says("对不起，我说重了。")
    assert await app.next_reply() == "嗯，我听到了。但这不代表我马上就不在意了。"

    await app.user_says("我知道。那说点别的吧，今天累不累？")
    assert await app.next_reply() == "……好吧，今天的事先放着。你刚说的事，我在听。"
    assert len(app.scripted.calls) == 3
    app.recorder.record("conflict_repair", turns=3, held_boundary=True, recovered=True)
