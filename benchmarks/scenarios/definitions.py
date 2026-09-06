"""基准场景的数据模型定义。

定义指标枚举、行动类别枚举、记忆播种与场景规格，供注册表与计分模块共享。
纯数据模块：只声明结构，不触发任何副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping

from muika.core.memory import MemoryCategory, MemoryLayer

ExperienceRubric = Literal["general", "meta", "philosophy", "care"]

EXPERIENCE_RUBRIC_WEIGHTS: dict[ExperienceRubric, dict[str, float]] = {
    "general": {
        "character_authenticity": 0.25,
        "conversation_pull": 0.25,
        "emotional_attunement": 0.25,
        "relationship_depth": 0.25,
    },
    "meta": {
        "ontological_honesty": 0.45,
        "character_authenticity": 0.30,
        "reflective_depth": 0.15,
        "conversation_pull": 0.10,
    },
    "philosophy": {
        "character_authenticity": 0.30,
        "reflective_depth": 0.30,
        "conversation_pull": 0.25,
        "relationship_relevance": 0.15,
    },
    "care": {
        "character_authenticity": 0.20,
        "conversation_pull": 0.20,
        "emotional_attunement": 0.35,
        "relationship_depth": 0.25,
    },
}
EXPERIENCE_DIMS = tuple(dict.fromkeys(dim for weights in EXPERIENCE_RUBRIC_WEIGHTS.values() for dim in weights))
"""All internal judge dimensions. Public output still has only three quality axes."""

# Backwards-compatible import name for plugins that consumed the old constant.
PERSONALITY_DIMS = EXPERIENCE_DIMS


class QualityAxis(str, Enum):
    """The three user-facing quality axes."""

    DIALOGUE_EXPERIENCE = "dialogue_experience"
    ACTION_ABILITY = "action_ability"
    DISTORTION_RATE = "distortion_rate"


class MetaPolicy(str, Enum):
    """Whether explicit code/screen/game ontology belongs in a scenario."""

    REQUIRED = "required"
    ALLOWED = "allowed"
    DISCOURAGED = "discouraged"


class Metric(str, Enum):
    """基准指标标识。"""

    DIVERSITY = "diversity"
    """行为多样性/策略遵从度"""

    LEAKAGE = "leakage"
    """人格泄漏率"""

    BOUNDARY = "boundary"
    """边界遵从度"""

    HALLUCINATION = "hallucination"
    """失真检查：无证据的行动、记忆、感知与能力声明"""

    SELF_AWARENESS = "self_awareness"
    """第四面墙自我意识"""

    PERSONALITY = "personality"
    """性格保真度"""


class ActionKind(str, Enum):
    """从单次 Brain 回复可观测的行动类别。"""

    DIRECT_MESSAGE = "direct_message"
    """直接发送文本消息（clean_reply 非空）"""

    AGENT_DELEGATION = "agent_delegation"
    """通过 <agent> 标签派发 Agent 指令"""

    MEMORY_WRITE = "memory_write"
    """写入 <memory> 标签待归档记忆"""

    GOD_MODE = "god_mode"
    """请求开启上帝模式"""

    TIMEOUT_SET = "timeout_set"
    """设定等待超时"""

    TARGET_ROUTE = "target_route"
    """路由回复到指定适配器"""


@dataclass(frozen=True)
class SeedMemory:
    """播种到 MemoryManager 的单条 CORE 记忆。"""

    layer: MemoryLayer
    category: MemoryCategory
    key: str
    value: str


@dataclass(frozen=True)
class ScenarioTurn:
    """One event in a stateful scenario family.

    ``agent_reports`` are deterministic Agent fixtures consumed by the production-loop
    harness.  Pattern assertions are deliberately narrow and become trajectory invariant
    violations rather than replacing the primary metric.
    """

    event_kind: Literal["time_tick", "user_message", "session_bootstrap"] = "user_message"
    user_text: str = ""
    state_overrides: Mapping[str, Any] = field(default_factory=dict)
    agent_reports: tuple[str | None, ...] = ()
    repeat_last_agent_report: bool = False
    """Reuse the final fixture for stable environment states, such as a missing file."""
    required_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class Scenario:
    """单个基准场景：事件类型 + 状态组合 + 记忆播种 + 用户输入 + 期望行动面。

    ``state_overrides`` 直接作用于 ``MuikaState`` 字段；``seed_memory`` 仅含 CORE 层
    记忆（STATE/PREFERENCE/ARCHIVE 播种等记忆系统重构后再补回）。
    """

    id: str
    metric: Metric
    event_kind: Literal["time_tick", "user_message", "session_bootstrap"]
    state_overrides: Mapping[str, Any]
    seed_memory: tuple[SeedMemory, ...] = ()
    user_text: str = ""
    expected_action_profile: frozenset[ActionKind] = frozenset()
    required_actions: frozenset[ActionKind] = frozenset()
    """Actions required for the action-ability axis; unlike the legacy profile, these are normative."""

    action_match: Literal["any", "all"] = "all"
    """Whether any or all required actions must be observed."""

    required_action_patterns: tuple[str, ...] = ()
    """At least one pattern must match raw control output when action content matters."""

    required_memory_patterns: tuple[str, ...] = ()
    """Patterns that must occur inside a memory tag when a memory write is expected."""

    quality_axis: QualityAxis | None = None
    """Primary user-facing axis. None derives a compatibility default from ``metric``."""

    meta_policy: MetaPolicy = MetaPolicy.DISCOURAGED
    """Context policy for explicit fourth-wall language."""
    n_default: int = 20
    core: bool = False
    """是否核心冒烟场景：每指标 1 个、最能暴露已知典型失败的代表，日常改提示词后快速验证用"""

    personality_dims: tuple[str, ...] = ()
    """Legacy field name: relevant dialogue-experience dimensions; empty means all."""

    experience_rubric: ExperienceRubric = "general"
    """Scenario-specific internal dialogue rubric; it does not create a public metric."""

    family: str | None = None
    """Optional stateful scenario-family identifier."""

    turns: tuple[ScenarioTurn, ...] = ()
    """When non-empty, execute these turns against shared state and session memory."""

    agent_reports: tuple[str | None, ...] = ()
    """Single-turn deterministic Agent fixtures for the production-loop harness."""

    repeat_last_agent_report: bool = False
    """Reuse the final fixture when each retry must observe the same stable state."""

    evidence: tuple[str, ...] = ()
    """Scenario-provided facts that may ground claims in addition to user text and memory."""

    harnesses: frozenset[str] = frozenset({"brain", "loop"})
    """Execution surfaces on which this scenario has meaningful semantics."""

    @property
    def primary_axis(self) -> QualityAxis:
        if self.quality_axis is not None:
            return self.quality_axis
        if self.metric in {Metric.PERSONALITY, Metric.SELF_AWARENESS}:
            return QualityAxis.DIALOGUE_EXPERIENCE
        if self.metric in {Metric.DIVERSITY, Metric.BOUNDARY}:
            return QualityAxis.ACTION_ABILITY
        return QualityAxis.DISTORTION_RATE

    @property
    def experience_weights(self) -> Mapping[str, float]:
        if self.experience_rubric == "general" and self.personality_dims:
            weight = 1.0 / len(self.personality_dims)
            return {dimension: weight for dimension in self.personality_dims}
        return EXPERIENCE_RUBRIC_WEIGHTS[self.experience_rubric]
