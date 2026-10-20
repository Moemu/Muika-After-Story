"""``TopicStore``（临时 YAML 注入）与 ``TopicManager.get_next_topic`` 测试。"""

import asyncio
import random
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from muika.config import mas_config
from muika.core import topic_manager as topic_manager_module
from muika.core.actions.tools import _topics as topic_tools
from muika.core.state import ActiveTopicState, MuikaState
from muika.core.topic_manager import EventTopic, TopicManager, TopicSource, TopicStore
from muika.database.crud import TopicHistoryCRUD


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


@pytest.mark.parametrize(
    ("entry", "categories", "category", "content"),
    [
        ({"id": "a1", "category": "trivia", "concept": "moon"}, ["trivia"], "trivia", "moon"),
        ({"id": "a1", "concept": "moon"}, ["misc"], "misc", "moon"),
        ({"id": "a1", "content": "star"}, ["misc"], "misc", "star"),
    ],
    ids=["indexed", "concept_field", "content_fallback"],
)
def test_topic_store_loads_and_indexes(tmp_path, entry, categories, category, content):
    store = TopicStore(_write_topics(tmp_path, [entry]))
    assert store.categories() == categories
    assert [t.id for t in store.get_by_category(category)] == ["a1"]
    assert store.get_by_category(category)[0].content == content
    assert store.is_empty() is False


def test_topic_store_missing_file_empty(tmp_path):
    store = TopicStore(tmp_path / "nope.yml")
    assert store.is_empty() is True


def test_topic_store_invalid_yaml_empty(tmp_path):
    p = tmp_path / "topics.yml"
    p.write_text("not: [valid", encoding="utf-8")
    store = TopicStore(p)
    assert store.is_empty() is True


def test_topic_store_prefers_user_override_and_falls_back_to_builtin(tmp_path, monkeypatch):
    user_path = tmp_path / "configs/topics.yml"
    builtin_path = _write_topics(tmp_path, [{"id": "builtin", "concept": "default"}])
    monkeypatch.setattr(topic_manager_module, "TOPICS_PATH", user_path)
    monkeypatch.setattr(topic_manager_module, "BUILTIN_TOPICS_PATH", builtin_path)
    store = TopicStore()
    assert store.get_by_category("misc")[0].id == "builtin"

    user_path.parent.mkdir()
    user_path.write_text(yaml.safe_dump({"topics": [{"id": "user", "concept": "custom"}]}), encoding="utf-8")
    store.reload()

    assert store.get_by_category("misc")[0].id == "user"


@pytest.mark.asyncio
async def test_topic_mutations_hold_lock_until_write(tmp_path, monkeypatch):
    topic_path = tmp_path / "topics.yml"
    topic_path.write_text(
        "topics:\n  - id: first\n    category: trivia\n    concept: old\n    cooldown_days: 7\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(topic_tools, "TOPICS_PATH", topic_path)
    monkeypatch.setattr(topic_tools, "BUILTIN_TOPICS_PATH", topic_path)
    monkeypatch.setattr(topic_tools, "_TOPICS_LOCK", asyncio.Lock())
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    active = 0
    max_active = 0

    async def delayed_write(new_text: str, reason: str, action: str) -> str:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        topic_path.write_text(new_text, encoding="utf-8")
        active -= 1
        return reason + action

    monkeypatch.setattr(topic_tools, "_apply_topics_change", delayed_write)

    await asyncio.gather(
        topic_tools.topic_update("first", concept="new", reason="update"),
        topic_tools.topic_add("second", "trivia", "second", reason="add"),
    )

    data = yaml.safe_load(topic_path.read_text(encoding="utf-8"))
    assert max_active == 1
    assert {item["id"] for item in data["topics"]} == {"first", "second"}
    assert data["topics"][0]["concept"] == "new"


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
async def test_get_next_topic_weighted_selection(tmp_path, redirect_get_session):
    p = _write_topics(
        tmp_path,
        [
            {"id": "a1", "category": "trivia", "concept": "moon"},
            {"id": "a2", "category": "trivia", "concept": "mind"},
        ],
    )
    tm = _manager_with_store(TopicStore(p))
    # a2 有低互动历史且已过冷却：候选权重必须来自真实历史，而不是测试注入
    await TopicHistoryCRUD.record(redirect_get_session, topic_id="a2", user_engaged=False)
    await TopicHistoryCRUD.record(redirect_get_session, topic_id="a2", user_engaged=False)
    row = await TopicHistoryCRUD.get_by_topic_id(redirect_get_session, "a2")
    assert row is not None and row.use_count == 2 and row.engaged_count == 0
    row.last_used_at = (datetime.now() - timedelta(days=8)).isoformat()
    await redirect_get_session.commit()

    random.seed(20261020)
    chosen_ids = [(await tm.get_next_topic(MuikaState())).id for _ in range(100)]
    assert set(chosen_ids) == {"a1", "a2"}
    assert chosen_ids.count("a1") > chosen_ids.count("a2")
    assert list(tm._recent_categories) == ["trivia", "trivia", "trivia"]


@pytest.mark.asyncio
async def test_get_next_topic_all_cooldown_none(tmp_path, redirect_get_session):
    p = _write_topics(tmp_path, [{"id": "a1", "category": "trivia", "concept": "moon", "cooldown_days": 7}])
    tm = _manager_with_store(TopicStore(p))
    assert tm.store.categories() == ["trivia"]

    await tm.record_topic_used("a1", user_engaged=False)

    assert await tm.get_next_topic(MuikaState()) is None


@pytest.mark.asyncio
async def test_record_topic_used_writes_db(tmp_path, redirect_get_session):
    tm = _manager_with_store(TopicStore(tmp_path / "empty.yml"))
    await tm.record_topic_used("t1", user_engaged=False)
    await tm.record_topic_used("t1", user_engaged=True)

    row = await TopicHistoryCRUD.get_by_topic_id(redirect_get_session, "t1")
    assert row is not None
    assert row.use_count == 2
    assert row.engaged_count == 1
