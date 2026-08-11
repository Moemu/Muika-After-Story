"""``muika/database/crud.py`` 全部静态方法测试（喂临时 SQLite session）。

CRUD 方法不 commit（由真实 ``get_session`` 上下文管理），测试中在写入后显式 commit。
"""

from datetime import datetime, timedelta

from muika.database.crud import (
    ArchiveCRUD,
    MemoryRecordCRUD,
    RssDigestCacheCRUD,
    TopicHistoryCRUD,
    UsageORM,
)


def _today() -> str:
    return datetime.now().strftime("%Y.%m.%d")


# ---------------------------------------------------------------------------
# UsageORM
# ---------------------------------------------------------------------------


async def test_usage_save_and_get(db_session):
    await UsageORM.save_usage(db_session, plugin="brain", model="m", input_tokens=10, output_tokens=5, cached_tokens=2)
    await db_session.commit()
    total = await UsageORM.get_usage(db_session, plugin="brain", date=_today(), model="m")
    assert total == 17

    # 同日同 key 累加
    await UsageORM.save_usage(db_session, plugin="brain", model="m", input_tokens=1, output_tokens=1, cached_tokens=0)
    await db_session.commit()
    total = await UsageORM.get_usage(db_session, plugin="brain", date=_today(), model="m")
    assert total == 19


async def test_usage_save_negative_ignored(db_session):
    await UsageORM.save_usage(db_session, plugin="p", model="m", input_tokens=-1, output_tokens=0, cached_tokens=0)
    await db_session.commit()
    total = await UsageORM.get_usage(db_session, plugin="p", date=None, model=None)
    assert total == 0


async def test_usage_get_records_recent(db_session):
    await UsageORM.save_usage(db_session, plugin="p", model="m", input_tokens=1, output_tokens=0, cached_tokens=0)
    await db_session.commit()
    rows = await UsageORM.get_usage_records(db_session, days=7)
    assert len(rows) == 1
    assert rows[0].plugin == "p"


# ---------------------------------------------------------------------------
# MemoryRecordCRUD
# ---------------------------------------------------------------------------


async def test_memory_record_upsert_create(db_session):
    await MemoryRecordCRUD.upsert(db_session, layer="core", category="user", key="name", value="Alice")
    await db_session.commit()
    rows = await MemoryRecordCRUD.get_all(db_session)
    assert len(rows) == 1
    assert rows[0].key == "name"
    assert rows[0].value == "Alice"


async def test_memory_record_upsert_update(db_session):
    await MemoryRecordCRUD.upsert(db_session, layer="core", category="user", key="name", value="Alice")
    await MemoryRecordCRUD.upsert(db_session, layer="core", category="user", key="name", value="Bob")
    await db_session.commit()
    rows = await MemoryRecordCRUD.get_all(db_session)
    assert len(rows) == 1
    assert rows[0].value == "Bob"


async def test_memory_record_delete_true_false(db_session):
    await MemoryRecordCRUD.upsert(db_session, layer="core", category="user", key="k", value="v")
    await db_session.commit()
    assert await MemoryRecordCRUD.delete(db_session, layer="core", key="k") is True
    await db_session.commit()
    assert await MemoryRecordCRUD.delete(db_session, layer="core", key="k") is False


# ---------------------------------------------------------------------------
# ArchiveCRUD
# ---------------------------------------------------------------------------


async def test_archive_add_and_list(db_session):
    await ArchiveCRUD.add(
        db_session, session_id="s1", summary="sum", period_start="2026-01-01", period_end="2026-01-02"
    )
    await ArchiveCRUD.add(
        db_session, session_id="s2", summary="sum2", period_start="2026-01-03", period_end="2026-01-04"
    )
    await db_session.commit()
    rows = await ArchiveCRUD.list_all(db_session)
    assert len(rows) == 2
    assert rows[0].session_id == "s1"  # period_start 升序


async def test_archive_update_existing(db_session):
    await ArchiveCRUD.add(
        db_session, session_id="s1", summary="old", period_start="2026-01-01", period_end="2026-01-02"
    )
    await db_session.commit()
    await ArchiveCRUD.updated(
        db_session, session_id="s1", summary="new", period_start="2026-01-01", period_end="2026-01-02"
    )
    await db_session.commit()
    rows = await ArchiveCRUD.list_all(db_session)
    assert len(rows) == 1
    assert rows[0].summary == "new"


async def test_archive_update_missing_creates(db_session):
    await ArchiveCRUD.updated(
        db_session, session_id="s9", summary="new", period_start="2026-01-01", period_end="2026-01-02"
    )
    await db_session.commit()
    rows = await ArchiveCRUD.list_all(db_session)
    assert len(rows) == 1
    assert rows[0].summary == "new"


# ---------------------------------------------------------------------------
# TopicHistoryCRUD
# ---------------------------------------------------------------------------


async def test_topic_history_record_create_increment(db_session):
    await TopicHistoryCRUD.record(db_session, topic_id="t1", user_engaged=False)
    await db_session.commit()
    await TopicHistoryCRUD.record(db_session, topic_id="t1", user_engaged=True)
    await db_session.commit()
    row = await TopicHistoryCRUD.get_by_topic_id(db_session, "t1")
    assert row is not None
    assert row.use_count == 2
    assert row.engaged_count == 1


async def test_topic_history_get_by_topic_id_miss_and_hit(db_session):
    assert await TopicHistoryCRUD.get_by_topic_id(db_session, "missing") is None
    await TopicHistoryCRUD.record(db_session, topic_id="t1", user_engaged=False)
    await db_session.commit()
    assert await TopicHistoryCRUD.get_by_topic_id(db_session, "t1") is not None


# ---------------------------------------------------------------------------
# RssDigestCacheCRUD
# ---------------------------------------------------------------------------


async def _upsert_rss(db_session, topic_id: str):
    await RssDigestCacheCRUD.upsert(
        db_session,
        topic_id=topic_id,
        source_id="hn",
        title="T",
        link="http://example.com/x",
        published=None,
        score=80,
        keep=True,
        reason="good",
        primary_theme="tech",
        summary="s",
    )
    await db_session.commit()


async def test_rss_cache_upsert_get(db_session):
    await _upsert_rss(db_session, "hash1")
    cached = await RssDigestCacheCRUD.get_cached(db_session, topic_id="hash1", ttl_days=7)
    assert cached is not None
    assert cached.score == 80


async def test_rss_cache_expired_get_none(db_session):
    await _upsert_rss(db_session, "hash1")
    row = await RssDigestCacheCRUD.get_cached(db_session, topic_id="hash1", ttl_days=365)
    assert row is not None
    row.evaluated_at = (datetime.now() - timedelta(days=8)).isoformat()
    await db_session.commit()

    cached = await RssDigestCacheCRUD.get_cached(db_session, topic_id="hash1", ttl_days=7)
    assert cached is None


async def test_rss_cache_delete_expired(db_session):
    await _upsert_rss(db_session, "a")
    await _upsert_rss(db_session, "b")
    row_a = await RssDigestCacheCRUD.get_cached(db_session, topic_id="a", ttl_days=365)
    row_a.evaluated_at = (datetime.now() - timedelta(days=10)).isoformat()
    await db_session.commit()

    deleted = await RssDigestCacheCRUD.delete_expired(db_session, ttl_days=7)
    assert deleted == 1
    await db_session.commit()

    assert await RssDigestCacheCRUD.get_cached(db_session, topic_id="a", ttl_days=365) is None
    assert await RssDigestCacheCRUD.get_cached(db_session, topic_id="b", ttl_days=365) is not None
