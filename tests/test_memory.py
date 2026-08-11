"""``MemoryManager`` 纯内存记录与 prompt 构建测试，以及 DB 持久化（重定向 get_session）。

注意：``upsert_memory`` 等持久化方法在函数体内 ``from muika.database.db import get_session``
（运行时重新绑定），因此必须 patch ``muika.database.db.get_session`` 而不是
``muika.core.memory.get_session``。
"""

from datetime import datetime, timedelta

import pytest

from muika.core.memory import (
    ArchiveEntry,
    MemoryCategory,
    MemoryLayer,
    MemoryManager,
    MemoryRecord,
)


def _manager(max_turns: int = 3) -> MemoryManager:
    return MemoryManager(max_turns=max_turns)


# ---------------------------------------------------------------------------
# 内存记录管理
# ---------------------------------------------------------------------------


def test_add_context_and_maxlen_eviction():
    m = _manager()
    for i in range(5):
        m.add_context("user", f"msg {i}")
    assert len(m.recent_turns) == 3
    assert m.recent_turns[0].content == "msg 2"


@pytest.mark.asyncio
async def test_upsert_memory_in_memory():
    m = _manager()
    await m.upsert_memory(MemoryLayer.CORE, MemoryCategory.USER, "name", "Alice")
    assert "core:user:name" in m.records
    assert m.records["core:user:name"].value == "Alice"

    await m.upsert_memory(MemoryLayer.CORE, MemoryCategory.USER, "name", "Bob")
    assert m.records["core:user:name"].value == "Bob"


@pytest.mark.asyncio
async def test_forget_memory_in_memory():
    m = _manager()
    await m.upsert_memory(MemoryLayer.STATE, MemoryCategory.RELATION, "k", "v")
    await m.forget_memory(MemoryLayer.STATE, MemoryCategory.RELATION, "k")
    assert "state:relation:k" not in m.records

    await m.forget_memory(MemoryLayer.CORE, MemoryCategory.USER, "missing")  # no-op
    assert len(m.records) == 0


def test_new_session_first_vs_resume():
    m = _manager()
    assert m.session.is_first_session is True

    m.records["core:user:x"] = MemoryRecord(layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="x", value="1")
    m.add_context("user", "hi")
    m.new_session()
    assert m.session.is_first_session is False
    assert len(m.recent_turns) == 0


# ---------------------------------------------------------------------------
# prompt 构建
# ---------------------------------------------------------------------------


def test_get_core_prompt_empty():
    assert _manager().get_core_prompt() == ""


def test_get_core_prompt_groups_by_category():
    m = _manager()
    m.records["core:user:name"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="name", value="Alice"
    )
    m.records["core:self:is_ai"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.SELF, key="is_ai", value="true"
    )
    prompt = m.get_core_prompt()
    assert "## User (Core Facts)" in prompt
    assert "## Self (Core Facts)" in prompt
    assert "- name: Alice" in prompt
    assert "- is_ai: true" in prompt


def test_get_resume_context_first_session_empty():
    assert _manager().get_resume_context() == ""


def test_get_resume_context_sorted_and_limited():
    m = _manager()
    m.session.is_first_session = False
    for i in range(4):
        m.records[f"state:relation:k{i}"] = MemoryRecord(
            layer=MemoryLayer.STATE,
            category=MemoryCategory.RELATION,
            key=f"k{i}",
            value=str(i),
            updated_at=datetime(2026, 1, 1, 0, i),
        )
    prompt = m.get_resume_context(max_items=3)
    assert "## Recent Relationship State" in prompt
    lines = [line for line in prompt.splitlines() if line.startswith("- ")]
    assert len(lines) == 3
    assert lines[0] == "- k3: 3"  # 按 updated_at 降序取前 3 条


def test_iter_layer_skips_expired_state():
    m = _manager()
    m.records["state:relation:expired"] = MemoryRecord(
        layer=MemoryLayer.STATE,
        category=MemoryCategory.RELATION,
        key="expired",
        value="x",
        expires_at=datetime.now() - timedelta(hours=1),
    )
    m.records["core:user:keep"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="keep", value="y"
    )
    assert list(m._iter_layer(MemoryLayer.STATE)) == []
    assert len(list(m._iter_layer(MemoryLayer.CORE))) == 1


def test_get_archive_prompt_first_session_empty():
    assert _manager().get_archive_prompt() == ""


def test_get_archive_prompt_resume_mode():
    m = _manager()
    m.session.is_first_session = False
    m.archives.append(
        ArchiveEntry(
            session_id="s1",
            summary="sum A",
            period_start=datetime(2026, 1, 1),
            period_end=datetime(2026, 1, 2),
        )
    )
    m.archives.append(
        ArchiveEntry(
            session_id="s2",
            summary="sum B",
            period_start=datetime(2026, 1, 3),
            period_end=datetime(2026, 1, 4),
        )
    )
    prompt = m.get_archive_prompt(max_items=3)
    assert "## Recent Session Archives" in prompt
    assert "sum B" in prompt
    assert prompt.index("sum B") < prompt.index("sum A")  # period_end 降序


def test_get_memory_prompt_composes_layers():
    m = _manager()
    m.session.is_first_session = False
    m.records["core:user:name"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="name", value="Alice"
    )
    m.records["state:relation:mood"] = MemoryRecord(
        layer=MemoryLayer.STATE, category=MemoryCategory.RELATION, key="mood", value="warm"
    )
    m.archives.append(
        ArchiveEntry(
            session_id="s1",
            summary="sum",
            period_start=datetime(2026, 1, 1),
            period_end=datetime(2026, 1, 2),
        )
    )
    prompt = m.get_memory_prompt()
    assert "## User (Core Facts)" in prompt
    assert "## Recent Relationship State" in prompt
    assert "## Recent Session Archives" in prompt
    assert "\n" in prompt


def test_get_preference_records_filters_layer():
    m = _manager()
    m.records["preference:user:tea"] = MemoryRecord(
        layer=MemoryLayer.PREFERENCE, category=MemoryCategory.USER, key="tea", value="oolong"
    )
    m.records["core:user:name"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="name", value="Alice"
    )
    prefs = m.get_preference_records()
    assert len(prefs) == 1
    assert prefs[0].key == "tea"


def test_get_archives():
    m = _manager()
    m.archives.append(
        ArchiveEntry(session_id="s1", summary="x", period_start=datetime(2026, 1, 1), period_end=datetime(2026, 1, 2))
    )
    assert m.get_archives() is m.archives


# ---------------------------------------------------------------------------
# DB 持久化（重定向 muika.database.db.get_session）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_memory_persists_to_db(redirect_get_session):
    from muika.database.crud import MemoryRecordCRUD

    m = _manager()
    await m.upsert_memory(MemoryLayer.CORE, MemoryCategory.USER, "name", "Alice")

    rows = await MemoryRecordCRUD.get_all(redirect_get_session)
    assert len(rows) == 1
    assert rows[0].key == "name"
    assert rows[0].value == "Alice"


@pytest.mark.asyncio
async def test_add_archive_and_update_persist(redirect_get_session):
    from muika.database.crud import ArchiveCRUD

    m = _manager()
    await m.add_archive("summary1", datetime(2026, 1, 1), datetime(2026, 1, 2))
    rows = await ArchiveCRUD.list_all(redirect_get_session)
    assert len(rows) == 1
    assert rows[0].summary == "summary1"

    await m.update_archive("summary2", datetime(2026, 1, 1), datetime(2026, 1, 2))
    rows = await ArchiveCRUD.list_all(redirect_get_session)
    assert len(rows) == 1
    assert rows[0].summary == "summary2"
