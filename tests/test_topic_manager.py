"""``TopicStore``（临时 YAML 注入）与 ``TopicManager.get_next_topic`` 测试。"""

from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from muika.core.state import ActiveTopicState, MuikaState
from muika.core.topic_manager import (
    EventTopic,
    TopicManager,
    TopicSource,
    TopicStore,
)


def _write_topics(tmp_path: Path, topics: list[dict]) -> Path:
    p = tmp_path / "topics.yml"
    p.write_text(yaml.safe_dump({"topics": topics}), encoding="utf-8")
    return p


def _manager_with_store(store: TopicStore) -> TopicManager:
    """用 ``__new__`` 构造 TopicManager，手动注入 store 与内部队列。"""
    tm = TopicManager.__new__(TopicManager)
    tm.store = store
    tm._recent_categories = deque(maxlen=3)
    tm._event_queue = deque()
    return tm


# ---------------------------------------------------------------------------
# TopicStore
# ---------------------------------------------------------------------------


def test_topic_store_loads_and_indexes(tmp_path):
    p = _write_topics(
        tmp_path,
        [
            {"id": "a1", "category": "trivia", "concept": "moon"},
            {"id": "a2", "category": "philosophy", "concept": "mind"},
        ],
    )
    store = TopicStore(p)
    assert set(store.categories()) == {"trivia", "philosophy"}
    assert [t.id for t in store.get_by_category("trivia")] == ["a1"]
    assert store.is_empty() is False


def test_topic_store_concept_field(tmp_path):
    p = _write_topics(tmp_path, [{"id": "a1", "concept": "moon"}])
    store = TopicStore(p)
    assert store.get_by_category("misc")[0].content == "moon"


def test_topic_store_content_field_fallback(tmp_path):
    p = _write_topics(tmp_path, [{"id": "a1", "content": "star"}])
    store = TopicStore(p)
    assert store.get_by_category("misc")[0].content == "star"


def test_topic_store_default_category_misc(tmp_path):
    p = _write_topics(tmp_path, [{"id": "a1", "concept": "x"}])
    store = TopicStore(p)
    assert store.categories() == ["misc"]


def test_topic_store_missing_file_empty(tmp_path):
    store = TopicStore(tmp_path / "nope.yml")
    assert store.is_empty() is True


def test_topic_store_invalid_yaml_empty(tmp_path):
    p = tmp_path / "topics.yml"
    p.write_text("not: [valid", encoding="utf-8")
    store = TopicStore(p)
    assert store.is_empty() is True


# ---------------------------------------------------------------------------
# TopicManager.get_next_topic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_next_topic_active_topic_blocks(tmp_path):
    tm = _manager_with_store(TopicStore(tmp_path / "empty.yml"))
    state = MuikaState(active_topic=ActiveTopicState(topic_id="t", topic_seed="s", topic_type="trivia"))
    assert await tm.get_next_topic(state) is None


@pytest.mark.asyncio
async def test_get_next_topic_event_queue_priority(tmp_path):
    tm = _manager_with_store(TopicStore(tmp_path / "empty.yml"))
    topic = EventTopic(id="e1", source=TopicSource.EVENT, category="news", title="T", content="C")
    tm.enqueue_event(topic)
    chosen = await tm.get_next_topic(MuikaState())
    assert chosen == topic
    assert len(tm._event_queue) == 0


@pytest.mark.asyncio
async def test_get_next_topic_empty_store_none(tmp_path):
    tm = _manager_with_store(TopicStore(tmp_path / "empty.yml"))
    assert await tm.get_next_topic(MuikaState()) is None


@pytest.mark.asyncio
async def test_get_next_topic_weighted_selection(tmp_path):
    p = _write_topics(tmp_path, [{"id": "a1", "category": "trivia", "concept": "moon"}])
    store = TopicStore(p)
    tm = _manager_with_store(store)

    async def fake_candidates():
        return {"trivia": [(t, 1.0) for t in store.get_by_category("trivia")]}

    tm._get_available_candidates = fake_candidates

    with patch(
        "muika.core.topic_manager.random.choices",
        side_effect=lambda *args, **kwargs: [args[0][0]],
    ):
        chosen = await tm.get_next_topic(MuikaState())

    assert chosen is not None
    assert chosen.id == "a1"
    assert "trivia" in tm._recent_categories


@pytest.mark.asyncio
async def test_get_next_topic_all_cooldown_none(tmp_path):
    p = _write_topics(tmp_path, [{"id": "a1", "category": "trivia", "concept": "moon"}])
    tm = _manager_with_store(TopicStore(p))

    async def fake_candidates():
        return {}

    tm._get_available_candidates = fake_candidates

    assert await tm.get_next_topic(MuikaState()) is None


@pytest.mark.asyncio
async def test_record_topic_used_writes_db(tmp_path, redirect_get_session):
    from muika.database.crud import TopicHistoryCRUD

    tm = _manager_with_store(TopicStore(tmp_path / "empty.yml"))
    await tm.record_topic_used("t1", user_engaged=False)
    await tm.record_topic_used("t1", user_engaged=True)

    row = await TopicHistoryCRUD.get_by_topic_id(redirect_get_session, "t1")
    assert row is not None
    assert row.use_count == 2
    assert row.engaged_count == 1
