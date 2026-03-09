from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml
from nonebot import logger
from nonebot_plugin_orm import get_scoped_session

from muika.database.crud import TopicHistoryCRUD

from .state import MuikaState

TOPIC_WEIGHTS: dict[str, float] = {
    "relationship": 0.35,
    "philosophy": 0.25,
    "trivia": 0.20,
    "story": 0.10,
    "meta": 0.05,
}

_TOPICS_PATH = Path(__file__).parent.parent.parent / "configs" / "topics.yml"

# 最近抽取的类型进入队列后，权重会被衰减，避免同一类型连续出现。
_RECENT_TYPE_PENALTY: float = 0.25
_RECENT_TYPE_WINDOW: int = 3


@dataclass
class TopicSeed:
    id: str
    type: str
    seed: str
    tags: list[str] = field(default_factory=list)
    cooldown_days: int = 7


class TopicStore:
    """从 YAML 文件加载并索引话题种子。"""

    def __init__(self, path: Path = _TOPICS_PATH) -> None:
        self._by_type: dict[str, list[TopicSeed]] = {}
        self._load(path)

    def _load(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            for entry in data.get("topics", []):
                seed = TopicSeed(
                    id=entry["id"],
                    type=entry["type"],
                    seed=entry["seed"],
                    tags=entry.get("tags", []),
                    cooldown_days=entry.get("cooldown_days", 7),
                )
                self._by_type.setdefault(seed.type, []).append(seed)
            total = sum(len(v) for v in self._by_type.values())
            logger.info(f"[TopicStore] Loaded {total} topic seeds across {len(self._by_type)} types")
        except FileNotFoundError:
            logger.warning(f"[TopicStore] topics.yml not found at {path} — topic system disabled")
        except Exception as e:
            logger.error(f"[TopicStore] Failed to load topics.yml: {e}")

    def get_by_type(self, topic_type: str) -> list[TopicSeed]:
        return self._by_type.get(topic_type, [])

    def types(self) -> list[str]:
        return list(self._by_type.keys())

    def is_empty(self) -> bool:
        return not self._by_type


class TopicManager:
    """话题选择与历史追踪的调度器。在 loop.py 的双管线架构中作为旁路入口。"""

    def __init__(self) -> None:
        self.store = TopicStore()
        self._recent_types: deque[str] = deque(maxlen=_RECENT_TYPE_WINDOW)

    async def get_next_topic(self, state: MuikaState) -> Optional[TopicSeed]:
        """
        根据情绪状态与冷却历史选择下一个话题种子。

        以下情况返回 None：
        - 当前已有活跃话题（state.active_topic is not None）
        - 所有候选话题均在冷却期内
        - 话题库为空
        """
        if state.active_topic is not None:
            return None

        if self.store.is_empty():
            return None

        # boredom 主导时偏向 trivia/story；curiosity 主导时偏向 philosophy/relationship
        weights = dict(TOPIC_WEIGHTS)
        if state.boredom > state.curiosity:
            weights["trivia"] = weights.get("trivia", 0.0) + 0.10
            weights["story"] = weights.get("story", 0.0) + 0.05
            weights["philosophy"] = max(0.0, weights.get("philosophy", 0.0) - 0.10)
            weights["meta"] = max(0.0, weights.get("meta", 0.0) - 0.05)

        # 按类型构建候选列表，过滤掉仍在冷却期内的话题，并记录个体 engaged 权重
        # candidates_by_type 存储 (TopicSeed, individual_weight) 元组
        candidates_by_type: dict[str, list[tuple[TopicSeed, float]]] = {}
        try:
            db_session = get_scoped_session()
            now = datetime.now()
            for ttype in self.store.types():
                valid: list[tuple[TopicSeed, float]] = []
                for t in self.store.get_by_type(ttype):
                    row = await TopicHistoryCRUD.get_by_topic_id(db_session, t.id)
                    if row:
                        last_used = datetime.fromisoformat(row.last_used_at)
                        if (now - last_used) < timedelta(days=t.cooldown_days):
                            continue
                        # engaged-rate 降权：用过 2 次以上但参与率低的话题降低被选中概率
                        ind_w = 1.0
                        if row.use_count >= 2:
                            rate = row.engaged_count / row.use_count
                            if rate < 0.3:
                                ind_w = 0.3
                            elif rate < 0.5:
                                ind_w = 0.6
                        valid.append((t, ind_w))
                    else:
                        valid.append((t, 1.0))
                if valid:
                    candidates_by_type[ttype] = valid
        except Exception as e:
            logger.error(f"[TopicManager] DB cooldown check failed: {e} — falling back to no-cooldown selection")
            candidates_by_type = {
                t: [(s, 1.0) for s in self.store.get_by_type(t)]
                for t in self.store.types()
                if self.store.get_by_type(t)
            }

        if not candidates_by_type:
            logger.debug("[TopicManager] All topics are in cooldown — skipping.")
            return None

        # 仅对有候选的类型重新归一化权重，并对最近已抽取的类型施加衰减惩罚
        filtered_weights = {t: weights.get(t, 0.05) for t in candidates_by_type}
        for recent_type in self._recent_types:
            if recent_type in filtered_weights:
                filtered_weights[recent_type] *= _RECENT_TYPE_PENALTY
        total = sum(filtered_weights.values())
        if total == 0:
            return None
        for k in filtered_weights:
            filtered_weights[k] /= total

        chosen_type = random.choices(
            list(filtered_weights.keys()),
            weights=list(filtered_weights.values()),
            k=1,
        )[0]

        # 在选定类型内按 individual_weight 加权选择
        type_candidates = candidates_by_type[chosen_type]
        chosen = random.choices(
            [c[0] for c in type_candidates],
            weights=[c[1] for c in type_candidates],
            k=1,
        )[0]
        self._recent_types.append(chosen_type)
        logger.debug(
            f"[TopicManager] Selected topic: {chosen.id!r} (type={chosen.type}) "
            f"recent_types={list(self._recent_types)}"
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
