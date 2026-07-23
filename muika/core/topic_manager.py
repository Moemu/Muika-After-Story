from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml
from nonebot_plugin_orm import get_scoped_session

from muika.database.crud import TopicHistoryCRUD
from muika.utils.logger import logger

from .state import MuikaState


class TopicSource(Enum):
    STATIC = "static"
    """内部预设哲学/彩蛋/关系话题"""
    EVENT = "event"
    """外部输入的动态事件话题（如 RSS 摘要）"""


@dataclass
class BaseTopic:
    id: str
    source: TopicSource
    category: str
    content: str = ""
    tags: list[str] = field(default_factory=list)
    cooldown_days: int = 7


@dataclass
class StaticTopic(BaseTopic):
    source = TopicSource.STATIC


@dataclass
class EventTopic(BaseTopic):
    source = TopicSource.EVENT
    title: str = ""
    content: str = ""
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))


TOPIC_WEIGHTS: dict[str, float] = {
    "relationship": 0.35,
    "philosophy": 0.25,
    "trivia": 0.20,
    "story": 0.10,
    "meta": 0.05,
}

_TOPICS_PATH = Path(__file__).parent.parent.parent / "configs" / "topics.yml"
_RECENT_TYPE_PENALTY: float = 0.25
_RECENT_TYPE_WINDOW: int = 3


class TopicStore:
    """从 YAML 文件加载并索引静态话题种子。"""

    def __init__(self, path: Path = _TOPICS_PATH) -> None:
        self._by_category: dict[str, list[StaticTopic]] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            for entry in data.get("topics", []):
                # 兼容旧字段：如果存在 content 则作为 content
                concept = entry.get("concept", entry.get("content", ""))
                topic = StaticTopic(
                    id=entry["id"],
                    source=TopicSource.STATIC,
                    category=entry.get("category", entry.get("type", "misc")),
                    content=concept,
                    tags=entry.get("tags", []),
                    cooldown_days=entry.get("cooldown_days", 7),
                )
                self._by_category.setdefault(topic.category, []).append(topic)
            total = sum(len(v) for v in self._by_category.values())
            logger.info(f"[TopicStore] Loaded {total} static topics across {len(self._by_category)} categories")
        except FileNotFoundError:
            logger.warning(f"[TopicStore] topics.yml not found at {path} — static topics disabled")
        except Exception as e:
            logger.error(f"[TopicStore] Failed to load topics.yml: {e}")

    def get_by_category(self, category: str) -> list[StaticTopic]:
        return self._by_category.get(category, [])

    def categories(self) -> list[str]:
        return list(self._by_category.keys())

    def is_empty(self) -> bool:
        return not self._by_category


class TopicManager:
    """话题选择与历史追踪的调度器。"""

    def __init__(self) -> None:
        self.store = TopicStore()
        self._recent_categories: deque[str] = deque(maxlen=_RECENT_TYPE_WINDOW)
        self._event_queue: deque[EventTopic] = deque()

    def enqueue_event(self, topic: EventTopic) -> None:
        """从外部（如 DigestAgent / 管家）注入动态新闻。"""
        self._event_queue.append(topic)

    async def _get_available_candidates(self) -> dict[str, list[tuple[StaticTopic, float]]]:
        """获取所有度过冷却期的话题，并根据历史互动率计算独立权重。"""
        candidates: dict[str, list[tuple[StaticTopic, float]]] = {}
        try:
            db_session = get_scoped_session()
            now = datetime.now()

            for category in self.store.categories():
                valid_topics: list[tuple[StaticTopic, float]] = []
                for topic in self.store.get_by_category(category):
                    history_record = await TopicHistoryCRUD.get_by_topic_id(db_session, topic.id)

                    if not history_record:
                        valid_topics.append((topic, 1.0))
                        continue

                    last_used_time = datetime.fromisoformat(history_record.last_used_at)
                    if (now - last_used_time) < timedelta(days=topic.cooldown_days):
                        continue

                    # 计算互动率权重惩罚：如果多次抛出但用户不理睬，则降低选中概率
                    topic_weight = 1.0
                    if history_record.use_count >= 2:
                        engagement_rate = history_record.engaged_count / history_record.use_count
                        if engagement_rate < 0.3:
                            topic_weight = 0.3
                        elif engagement_rate < 0.5:
                            topic_weight = 0.6

                    valid_topics.append((topic, topic_weight))

                if valid_topics:
                    candidates[category] = valid_topics
            return candidates
        except Exception as e:
            logger.error(f"[TopicManager] DB cooldown check failed: {e}")
            return {
                category: [(topic, 1.0) for topic in self.store.get_by_category(category)]
                for category in self.store.categories()
                if self.store.get_by_category(category)
            }

    async def get_next_topic(self, state: MuikaState) -> Optional[BaseTopic]:
        if state.active_topic is not None:
            return None

        # 优先级 1：外部动态事件
        if self._event_queue:
            topic = self._event_queue.popleft()
            logger.debug(f"[TopicManager] Popped event topic: {topic.id}")
            return topic

        # 优先级 2 & 3：根据权重和冷却机制从静态话题中选择
        if self.store.is_empty():
            return None

        weights = dict(TOPIC_WEIGHTS)
        if state.boredom > state.curiosity:
            weights["trivia"] = weights.get("trivia", 0.0) + 0.10
            weights["story"] = weights.get("story", 0.0) + 0.05
            weights["philosophy"] = max(0.0, weights.get("philosophy", 0.0) - 0.10)
            weights["meta"] = max(0.0, weights.get("meta", 0.0) - 0.05)

        candidates_by_cat = await self._get_available_candidates()

        if not candidates_by_cat:
            logger.debug("[TopicManager] All static topics in cooldown.")
            return None

        filtered_weights = {t: weights.get(t, 0.05) for t in candidates_by_cat}
        for recent_cat in self._recent_categories:
            if recent_cat in filtered_weights:
                filtered_weights[recent_cat] *= _RECENT_TYPE_PENALTY
        total = sum(filtered_weights.values())
        if total == 0:
            return None
        for k in filtered_weights:
            filtered_weights[k] /= total

        chosen_cat = random.choices(
            list(filtered_weights.keys()),
            weights=list(filtered_weights.values()),
            k=1,
        )[0]

        # 在选定类型内按 individual_weight 加权选择
        cat_candidates = candidates_by_cat[chosen_cat]
        chosen = random.choices(
            [c[0] for c in cat_candidates],
            weights=[c[1] for c in cat_candidates],
            k=1,
        )[0]
        self._recent_categories.append(chosen_cat)
        logger.debug(
            f"[TopicManager] Selected topic: {chosen.id!r} (category={chosen.category}) "
            f"recent_categories={list(self._recent_categories)}"
        )
        return chosen

    async def record_topic_used(self, topic_id: str, *, user_engaged: bool) -> None:
        """将话题使用记录写入 TopicHistory，供后续冷却期检查。"""
        try:
            db_session = get_scoped_session()
            await TopicHistoryCRUD.record(db_session, topic_id=topic_id, user_engaged=user_engaged)
            await db_session.commit()
            logger.debug(f"[TopicManager] Recorded topic {topic_id!r} (engaged={user_engaged})")
        except Exception as e:
            logger.error(f"[TopicManager] Failed to record topic history for {topic_id!r}: {e}")
