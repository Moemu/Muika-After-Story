from __future__ import annotations

import random
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


@dataclass
class TopicSeed:
    id: str
    type: str
    seed: str
    tags: list[str] = field(default_factory=list)
    cooldown_days: int = 7


class TopicStore:
    """Loads and indexes topic seeds from the YAML file."""

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
    """
    Orchestrates topic selection and history tracking.

    Serves as the interface between loop.py (Dual-Pipeline) and the topic subsystem.
    The Brain handles prompt construction; TopicManager owns scheduling logic only.
    """

    def __init__(self) -> None:
        self.store = TopicStore()

    async def get_next_topic(self, state: MuikaState) -> Optional[TopicSeed]:
        """
        Select the next topic seed based on emotional state and cooldown history.

        Returns None if:
        - A topic is already active (state.active_topic is not None)
        - All candidates are in cooldown
        - The topic store is empty
        """
        if state.active_topic is not None:
            return None

        if self.store.is_empty():
            return None

        # Bias weights toward trivia/story when boredom is the dominant driver,
        # toward philosophy/relationship when curiosity is the driver.
        weights = dict(TOPIC_WEIGHTS)
        if state.boredom > state.curiosity:
            weights["trivia"] = weights.get("trivia", 0.0) + 0.10
            weights["story"] = weights.get("story", 0.0) + 0.05
            weights["philosophy"] = max(0.0, weights.get("philosophy", 0.0) - 0.10)
            weights["meta"] = max(0.0, weights.get("meta", 0.0) - 0.05)

        # Build per-type candidate lists, filtering out topics still in cooldown.
        candidates_by_type: dict[str, list[TopicSeed]] = {}
        try:
            db_session = get_scoped_session()
            now = datetime.now()
            for ttype in self.store.types():
                valid: list[TopicSeed] = []
                for t in self.store.get_by_type(ttype):
                    row = await TopicHistoryCRUD.get_by_topic_id(db_session, t.id)
                    if row:
                        last_used = datetime.fromisoformat(row.last_used_at)
                        if (now - last_used) < timedelta(days=t.cooldown_days):
                            continue
                    valid.append(t)
                if valid:
                    candidates_by_type[ttype] = valid
        except Exception as e:
            logger.error(f"[TopicManager] DB cooldown check failed: {e} — falling back to no-cooldown selection")
            candidates_by_type = {t: self.store.get_by_type(t) for t in self.store.types() if self.store.get_by_type(t)}

        if not candidates_by_type:
            logger.debug("[TopicManager] All topics are in cooldown — skipping.")
            return None

        # Renormalize weights to available types only.
        filtered_weights = {t: weights.get(t, 0.05) for t in candidates_by_type}
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
        chosen = random.choice(candidates_by_type[chosen_type])
        logger.debug(f"[TopicManager] Selected topic: {chosen.id!r} (type={chosen.type})")
        return chosen

    async def record_topic_used(self, topic_id: str, *, user_engaged: bool) -> None:
        """Persist the topic usage to TopicHistory for future cooldown checks."""
        try:
            db_session = get_scoped_session()
            await TopicHistoryCRUD.record(db_session, topic_id=topic_id, user_engaged=user_engaged)
            await db_session.commit()
            logger.debug(f"[TopicManager] Recorded topic {topic_id!r} (engaged={user_engaged})")
        except Exception as e:
            logger.error(f"[TopicManager] Failed to record topic history for {topic_id!r}: {e}")
