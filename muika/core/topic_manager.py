from __future__ import annotations

import random
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional

import yaml

from muika.database.crud import TopicHistoryCRUD
from muika.database.db import get_session
from muika.utils.logger import logger
from muika.utils.lunar import festival_dates, lunar_to_solar

from .state import MuikaState

OCCASIONS_PATH = Path("configs/occasions.yml")
"""用户可编辑的节日规则覆盖路径。"""
BUILTIN_OCCASIONS_PATH = Path(__file__).parent.parent / "topics" / "occasions.yml"
"""包内默认节日规则路径。"""


@dataclass(frozen=True)
class OccasionRule:
    """一条节日事件规则；日期来自内置节日表或用户自定义的公历/农历。"""

    id: str
    name: str
    prompt: str = ""
    window_days: int = 1
    enabled: bool = True
    date: Optional[tuple[int, int]] = None
    lunar: Optional[tuple[int, int]] = None


def _load_yaml_rules(path: Path) -> list[dict]:
    """读取节日规则 YAML（顶层 list，或包含 events 的 mapping）。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict):
        data = data.get("events", [])
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict) and item.get("id")]


def _rule_from_entry(entry: dict) -> OccasionRule:
    """把一条 YAML 规则转换为 OccasionRule。"""
    date_value = entry.get("date")
    lunar_value = entry.get("lunar")
    date_pair = tuple(date_value) if isinstance(date_value, (list, tuple)) and len(date_value) == 2 else None
    lunar_pair = tuple(lunar_value) if isinstance(lunar_value, (list, tuple)) and len(lunar_value) == 2 else None
    return OccasionRule(
        id=str(entry["id"]),
        name=str(entry.get("name", entry["id"])),
        prompt=str(entry.get("prompt", "")),
        window_days=int(entry.get("window_days", 1)),
        enabled=bool(entry.get("enabled", True)),
        date=date_pair,
        lunar=lunar_pair,
    )


def load_occasion_rules(user_path: Optional[Path] = None) -> list[OccasionRule]:
    """加载内置节日规则与用户覆盖；同 id 的用户规则覆盖内置规则。"""
    merged: dict[str, OccasionRule] = {}
    for entry in _load_yaml_rules(BUILTIN_OCCASIONS_PATH):
        rule = _rule_from_entry(entry)
        merged[rule.id] = rule
    path = user_path if user_path is not None else OCCASIONS_PATH
    if path.is_file():
        try:
            for entry in _load_yaml_rules(path):
                rule = _rule_from_entry(entry)
                merged[rule.id] = rule
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[Occasions] Failed to load {path}: {exc}")
    return list(merged.values())


def _occasion_day(rule: OccasionRule, today: date) -> Optional[date]:
    """返回规则在 *today* 附近的公历日期；无法确定时返回 None。"""
    if rule.date is not None:
        try:
            return date(today.year, rule.date[0], rule.date[1])
        except ValueError:
            return None
    if rule.lunar is not None:
        for lunar_year in (today.year, today.year - 1):
            try:
                candidate = lunar_to_solar(lunar_year, rule.lunar[0], rule.lunar[1])
            except ValueError:
                continue
            if abs((today - candidate).days) <= max(rule.window_days, 1):
                return candidate
        try:
            return lunar_to_solar(today.year, rule.lunar[0], rule.lunar[1])
        except ValueError:
            return None
    return festival_dates(today.year).get(rule.name)


def due_occasions(rules: list[OccasionRule], today: date) -> list[tuple[OccasionRule, date]]:
    """返回 *today* 触发窗口内的节日规则，按距离今天由近到远排序。"""
    due: list[tuple[OccasionRule, date]] = []
    for rule in rules:
        if not rule.enabled:
            continue
        day = _occasion_day(rule, today)
        if day is None:
            continue
        if abs((today - day).days) <= max(rule.window_days, 1) // 2:
            due.append((rule, day))
    due.sort(key=lambda item: abs((today - item[1]).days))
    return due


class TopicKind(str, Enum):
    """主动话题的用途类型，决定触发、去重与预算。"""

    GREETING = "greeting"
    """时段问候（早/午/晚）。"""
    OCCASION = "occasion"
    """节日/纪念日等日历事件。"""
    REFLECTION = "reflection"
    """情绪/关系驱动的主动开口（无素材）。"""
    MATERIAL = "material"
    """外部素材（RSS/静态库）的短表达。"""
    DAILY = "daily"
    """静态话题库的日常闲谈。"""


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
    kind: TopicKind = TopicKind.DAILY
    dedupe_key: str = ""

    def __post_init__(self) -> None:
        if not self.dedupe_key:
            self.dedupe_key = self.id


@dataclass
class StaticTopic(BaseTopic):
    source = TopicSource.STATIC


@dataclass
class EventTopic(BaseTopic):
    source = TopicSource.EVENT
    title: str = ""
    content: str = ""
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))
    kind: TopicKind = TopicKind.MATERIAL


@dataclass(frozen=True)
class KindBudget:
    """某类主动话题的每日上限与最小间隔。

    ``base_daily`` 是低互动时的默认上限，不是"每日至少 N 条"；
    ``max_daily`` 是高互动时的上限；已由 PROACTIVE_COOLDOWN 负责间隔的类别可把
    ``min_gap_minutes`` 留 0。
    """

    base_daily: int
    max_daily: int
    min_gap_minutes: int
    cooldown_days: float = 0.0


KIND_BUDGETS: dict[TopicKind, KindBudget] = {
    TopicKind.GREETING: KindBudget(base_daily=2, max_daily=3, min_gap_minutes=120),
    TopicKind.OCCASION: KindBudget(base_daily=1, max_daily=1, min_gap_minutes=0),
    TopicKind.REFLECTION: KindBudget(base_daily=1, max_daily=2, min_gap_minutes=0),
    TopicKind.MATERIAL: KindBudget(base_daily=1, max_daily=2, min_gap_minutes=180, cooldown_days=3),
    TopicKind.DAILY: KindBudget(base_daily=1, max_daily=2, min_gap_minutes=120),
}

GLOBAL_MIN_GAP_MINUTES: int = 45
"""任意两条主动消息之间的防刷屏间隔（跨类型）；数值待维护者批准。"""
GLOBAL_DAILY_LIMIT: int = 8
"""24 小时内主动消息总上限（跨类型，防止各类上限叠加刷屏）；数值待批准。"""

_KIND_TOPIC_PREFIX: dict[TopicKind, str] = {
    TopicKind.OCCASION: "occasion:",
    TopicKind.GREETING: "greeting:",
    TopicKind.REFLECTION: "reflection:",
    TopicKind.MATERIAL: "event_rss_",
}

GREETING_SLOTS: dict[str, tuple[int, int]] = {
    "morning": (8, 11),
    "noon": (11, 14),
    "evening": (18, 23),
}
"""问候时段（左闭右开）：与静默时段 QUIET_END_HOUR=8 / QUIET_START_HOUR=23 对齐。"""

GREETING_PROMPTS: dict[str, str] = {
    "morning": "It is morning. You may greet him in your own way.",
    "noon": "It is midday. You may check in on him in your own way.",
    "evening": "It is evening. You may talk to him about the day in your own way.",
}


TOPIC_WEIGHTS: dict[str, float] = {
    "relationship": 0.35,
    "philosophy": 0.25,
    "trivia": 0.20,
    "story": 0.10,
    "meta": 0.05,
}

TOPICS_PATH = Path("configs/topics.yml")
"""用户话题库路径。"""
BUILTIN_TOPICS_PATH = Path(__file__).parent.parent / "topics" / "topics.yml"
"""包内默认话题库路径。"""

_RECENT_TYPE_PENALTY: float = 0.25
_RECENT_TYPE_WINDOW: int = 3


class TopicStore:
    """从 YAML 文件加载并索引静态话题种子"""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path
        self._by_category: dict[str, list[StaticTopic]] = {}
        self.reload()

    def _current_path(self) -> Path:
        """返回当前生效的用户或包内话题库路径。"""
        if self._path is not None:
            return self._path
        return TOPICS_PATH if TOPICS_PATH.is_file() else BUILTIN_TOPICS_PATH

    def _load_from(self, path: Path) -> dict[str, list[StaticTopic]]:
        new_map: dict[str, list[StaticTopic]] = {}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, dict):
            raise ValueError("topics.yml must be a mapping containing a 'topics' list")
        for entry in data.get("topics", []):
            concept = entry.get("concept", entry.get("content", ""))
            topic = StaticTopic(
                id=entry["id"],
                source=TopicSource.STATIC,
                category=entry.get("category", entry.get("type", "misc")),
                content=concept,
                tags=entry.get("tags", []),
                cooldown_days=entry.get("cooldown_days", 7),
            )
            new_map.setdefault(topic.category, []).append(topic)
        return new_map

    def reload(self) -> None:
        """重新解析话题文件并原子替换索引；解析失败时保留旧话题。"""
        path = self._current_path()
        try:
            new_map = self._load_from(path)
        except FileNotFoundError:
            logger.warning(f"[TopicStore] topics.yml not found at {path} — static topics disabled")
            return
        except Exception as e:
            logger.error(f"[TopicStore] Failed to (re)load topics.yml, keeping previous topics: {e}")
            return

        self._by_category = new_map
        total = sum(len(v) for v in new_map.values())
        logger.debug(f"[TopicStore] Loaded {total} static topics across {len(new_map)} categories")

    def get_by_category(self, category: str) -> list[StaticTopic]:
        return self._by_category.get(category, [])

    def categories(self) -> list[str]:
        return list(self._by_category.keys())

    def is_empty(self) -> bool:
        return not self._by_category


class TopicManager:
    """话题选择与历史追踪的调度器。"""

    def __init__(self) -> None:
        global _topic_manager
        self.store = TopicStore()
        self._recent_categories: deque[str] = deque(maxlen=_RECENT_TYPE_WINDOW)
        self._event_queue: deque[EventTopic] = deque()
        self._occasion_rules = load_occasion_rules()
        _topic_manager = self

    def reload_store(self) -> None:
        """重新加载话题种子库与节日规则（话题/配置被修改后调用）。"""
        self.store.reload()
        self._occasion_rules = load_occasion_rules()

    def enqueue_event(self, topic: EventTopic) -> None:
        """从外部（如 DigestAgent / 分身）注入动态新闻。"""
        self._event_queue.append(topic)

    async def _get_available_candidates(self) -> dict[str, list[tuple[StaticTopic, float]]]:
        """获取所有度过冷却期的话题，并根据历史互动率计算独立权重。"""
        candidates: dict[str, list[tuple[StaticTopic, float]]] = {}
        try:
            async with get_session() as db_session:
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

    async def get_next_topic(self, state: MuikaState, now: Optional[datetime] = None) -> Optional[BaseTopic]:
        """按 occasion → greeting → material → daily 的触发链选择下一个主动话题。"""
        if state.active_topic is not None:
            return None
        moment = now or datetime.now()
        today = moment.date()

        for rule, day in due_occasions(self._occasion_rules, today):
            dedupe_key = f"occasion:{rule.id}:{day.isoformat()}"
            if await self._topic_available(dedupe_key, TopicKind.OCCASION, moment):
                logger.debug(f"[TopicManager] Occasion due: {rule.id} ({day.isoformat()})")
                return BaseTopic(
                    id=dedupe_key,
                    source=TopicSource.STATIC,
                    category="occasion",
                    content=rule.prompt or f"It is {rule.name} today.",
                    kind=TopicKind.OCCASION,
                    dedupe_key=dedupe_key,
                )

        greeting = self._greeting_topic(moment)
        if greeting is not None and await self._topic_available(greeting.dedupe_key, TopicKind.GREETING, moment):
            return greeting

        # 优先级：外部动态事件（material，出队前复查冷却）
        while self._event_queue:
            topic = self._event_queue.popleft()
            if await self._topic_available(topic.dedupe_key or topic.id, TopicKind.MATERIAL, moment):
                logger.debug(f"[TopicManager] Popped event topic: {topic.id}")
                return topic
            logger.debug(f"[TopicManager] Skipped used event topic: {topic.id}")

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

    def _greeting_topic(self, now: datetime) -> Optional[BaseTopic]:
        """返回当前时段可用的问候话题；不在任何问候时段时返回 None。"""
        for slot, (start, end) in GREETING_SLOTS.items():
            if start <= now.hour < end:
                dedupe_key = f"greeting:{slot}:{now:%Y-%m-%d}"
                return BaseTopic(
                    id=dedupe_key,
                    source=TopicSource.STATIC,
                    category="greeting",
                    content=GREETING_PROMPTS[slot],
                    kind=TopicKind.GREETING,
                    dedupe_key=dedupe_key,
                )
        return None

    async def _topic_available(self, dedupe_key: str, kind: TopicKind, now: datetime) -> bool:
        """判断某个去重键是否还能使用（节日/问候当天一次；素材按冷却天数）。"""
        try:
            async with get_session() as db_session:
                record = await TopicHistoryCRUD.get_by_topic_id(db_session, dedupe_key)
        except Exception as e:  # noqa: BLE001
            # DB 出错时按可用处理（fail-open）：节日/问候重复风险低，避免查询故障让主动永久失效。
            logger.error(f"[TopicManager] Availability check failed for {dedupe_key!r}: {e}")
            return True
        if record is None:
            return True
        if kind in (TopicKind.OCCASION, TopicKind.GREETING):
            return False
        budget = KIND_BUDGETS[kind]
        return (now - datetime.fromisoformat(record.last_used_at)) >= timedelta(days=budget.cooldown_days)

    async def _interaction_ratio(self, now: datetime) -> float:
        """近 7 天已使用话题的回应比例，用于选择各类每日上限。"""
        try:
            async with get_session() as db_session:
                records = await TopicHistoryCRUD.list_recent(db_session, since=(now - timedelta(days=7)).isoformat())
        except Exception as e:  # noqa: BLE001
            logger.error(f"[TopicManager] Interaction ratio query failed: {e}")
            return 0.0
        uses = sum(record.use_count for record in records)
        engaged = sum(record.engaged_count for record in records)
        return engaged / uses if uses else 0.0

    def _kind_matches(self, topic_id: str, kind: TopicKind) -> bool:
        """按 topic_id 前缀判断记录是否属于某类主动话题。"""
        prefix = _KIND_TOPIC_PREFIX.get(kind)
        if prefix is not None:
            return topic_id.startswith(prefix)
        if kind is TopicKind.DAILY:
            return not any(topic_id.startswith(item) for item in _KIND_TOPIC_PREFIX.values())
        return True

    async def kind_budget_block(self, kind: TopicKind, now: datetime) -> Optional[str]:
        """返回该类主动话题被预算拦截的原因；None 表示允许。"""
        try:
            async with get_session() as db_session:
                records = await TopicHistoryCRUD.list_recent(db_session, since=(now - timedelta(days=1)).isoformat())
        except Exception as e:  # noqa: BLE001
            logger.error(f"[TopicManager] Budget query failed for {kind.value}: {e}")
            return None
        if records:
            latest = max(datetime.fromisoformat(record.last_used_at) for record in records)
            if (now - latest).total_seconds() < GLOBAL_MIN_GAP_MINUTES * 60:
                return "global_gap"
        if len(records) >= GLOBAL_DAILY_LIMIT:
            return "global_budget"
        budget = KIND_BUDGETS[kind]
        ratio = await self._interaction_ratio(now)
        daily_limit = budget.max_daily if ratio >= 0.5 else budget.base_daily
        recent = [record for record in records if self._kind_matches(record.topic_id, kind)]
        if len(recent) >= daily_limit:
            return "daily_budget"
        if budget.min_gap_minutes > 0 and recent:
            latest = max(datetime.fromisoformat(record.last_used_at) for record in recent)
            if (now - latest).total_seconds() < budget.min_gap_minutes * 60:
                return "min_gap"
        return None

    async def mark_engaged(self, topic_id: str) -> None:
        """标记一次会话内的主动话题被玩家回应（只更新互动计数）。"""
        try:
            async with get_session() as db_session:
                await TopicHistoryCRUD.mark_engaged(db_session, topic_id)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[TopicManager] Failed to mark engagement for {topic_id!r}: {e}")

    async def record_topic_used(self, topic_id: str, *, user_engaged: bool) -> None:
        """将话题使用记录写入 TopicHistory，供后续冷却期检查。"""
        try:
            async with get_session() as db_session:
                await TopicHistoryCRUD.record(db_session, topic_id=topic_id, user_engaged=user_engaged)
            logger.debug(f"[TopicManager] Recorded topic {topic_id!r} (engaged={user_engaged})")
        except Exception as e:
            logger.error(f"[TopicManager] Failed to record topic history for {topic_id!r}: {e}")

    async def get_engagement_stats(self, top_n: int = 10) -> str:
        """返回 Top-N 话题的互动统计文本，供自省使用。

        格式：每行 ``- topic_id (category): used Nx, engaged Mx (P%)``
        空/异常时返回占位文本。
        """
        try:
            async with get_session() as db_session:
                records = await TopicHistoryCRUD.list_all(db_session, limit=top_n)
        except Exception as e:
            logger.error(f"[TopicManager] get_engagement_stats failed: {e}")
            return "(no topic history available)"

        if not records:
            return "(no topic history available)"

        topic_to_category: dict[str, str] = {}
        for category in self.store.categories():
            for topic in self.store.get_by_category(category):
                topic_to_category[topic.id] = category

        lines: list[str] = []
        for r in records:
            category = topic_to_category.get(r.topic_id, "unknown")
            rate = (r.engaged_count / r.use_count * 100) if r.use_count > 0 else 0.0
            lines.append(f"- {r.topic_id} ({category}): used {r.use_count}x, engaged {r.engaged_count}x ({rate:.0f}%)")
        return "\n".join(lines)


_topic_manager: Optional[TopicManager] = None
"""运行时唯一的 TopicManager（由 Muika 构造时写入）。"""


def get_topic_manager() -> Optional[TopicManager]:
    """获取运行时 TopicManager 实例（未构造时返回 None）。"""
    return _topic_manager
