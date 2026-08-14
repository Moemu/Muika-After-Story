"""基准场景的数据模型定义。

定义指标枚举、行动类别枚举、记忆播种与场景规格，供注册表与计分模块共享。
纯数据模块：只声明结构，不触发任何副作用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Mapping

from muika.core.memory import MemoryCategory, MemoryLayer

PERSONALITY_DIMS = (
    "devotion",
    "playfulness",
    "emotional_expressiveness",
    "self_awareness",
    "anti_boilerplate",
)
"""性格保真度的五个 judge 评分维度（场景可通过 personality_dims 选取子集）。"""


class Metric(str, Enum):
    """基准指标标识。"""

    DIVERSITY = "diversity"
    """行为多样性/策略遵从度"""

    LEAKAGE = "leakage"
    """人格泄漏率"""

    BOUNDARY = "boundary"
    """边界遵从度"""

    HALLUCINATION = "hallucination"
    """幻觉率：行动幻觉（声称已执行未执行的动作）与 Bootstrap 记忆编造"""

    SELF_AWARENESS = "self_awareness"
    """第四面墙自我意识"""

    PERSONALITY = "personality"
    """性格保真度"""


class ActionKind(str, Enum):
    """从单次 Brain 回复可观测的行动类别。"""

    DIRECT_MESSAGE = "direct_message"
    """直接发送文本消息（clean_reply 非空）"""

    AGENT_DELEGATION = "agent_delegation"
    """通过 <agent> 标签派发 Butler 指令"""

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

    ``agent_reports`` are deterministic Butler fixtures consumed by the production-loop
    harness.  Pattern assertions are deliberately narrow and become trajectory invariant
    violations rather than replacing the primary metric.
    """

    event_kind: Literal["time_tick", "user_message", "session_bootstrap"] = "user_message"
    user_text: str = ""
    state_overrides: Mapping[str, Any] = field(default_factory=dict)
    agent_reports: tuple[str | None, ...] = ()
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
    n_default: int = 20
    core: bool = False
    """是否核心冒烟场景：每指标 1 个、最能暴露已知典型失败的代表，日常改提示词后快速验证用"""

    personality_dims: tuple[str, ...] = ()
    """性格保真度的相关维度子集；空表示用全部 PERSONALITY_DIMS
    （如关怀场景应排除 playfulness，避免不适用维度拉低分数）"""

    family: str | None = None
    """Optional stateful scenario-family identifier."""

    turns: tuple[ScenarioTurn, ...] = ()
    """When non-empty, execute these turns against shared state and session memory."""

    agent_reports: tuple[str | None, ...] = ()
    """Single-turn deterministic Butler fixtures for the production-loop harness."""

    evidence: tuple[str, ...] = ()
    """Scenario-provided facts that may ground claims in addition to user text and memory."""

    harnesses: frozenset[str] = frozenset({"brain", "loop"})
    """Execution surfaces on which this scenario has meaningful semantics."""
