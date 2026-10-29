"""多实例 dreaming 覆盖进度：同步来的副本参与覆盖起点，读取只暴露最完整的一份。"""

from datetime import date, datetime

import pytest

from muika.core.memory_models import DreamResult
from muika.database.db import get_session
from muika.database.orm_models import DiaryORM

pytestmark = pytest.mark.e2e


async def test_dream_continues_from_best_synced_coverage(core_app_factory):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    first = await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    night = await app.muika.memory.add_context("user", "晚安，明天见。", timestamp=datetime(2026, 10, 6, 22))
    await app.muika.memory.add_context("user", "睡前又想起一句话。", timestamp=datetime(2026, 10, 6, 23))
    assert await app.muika.memory.save_dream(day, DreamResult(diary="第一份日记。"), first, {f"experience:{first}"})
    async with get_session() as db:
        db.add(
            DiaryORM(
                source=f"pc-node:dream:{day.isoformat()}",
                day=day.isoformat(),
                created_at=datetime.now().isoformat(),
                covered_through=night,
                content="另一节点写下的日记。",
                source_refs="[]",
            )
        )
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream",
        text=DreamResult(diary="接着写下去的日记。").model_dump_json(),
        name="continued_dream",
    )
    assert await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    call = next(call for call in app.scripted.calls if call["name"] == "continued_dream")
    assert f"Previous coverage: experience IDs <= {night}." in call["prompt"]
    assert app.db_query("SELECT COUNT(*) AS total FROM diary") == [{"total": 2}]
    app.recorder.record("dream_continued", synced_coverage=night, prompt_coverage=night, diary_rows=2)
