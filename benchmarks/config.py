"""基准运行配置。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_TRIALS = 10
"""全场景/核心集跑批的默认试验次数"""

SINGLE_SCENARIO_TRIALS = 5
"""单场景快速验证的默认试验次数（CLI 未显式给 --trials 且只跑 1 个场景时）"""


@dataclass(frozen=True)
class ModelSpec:
    """候选模型规格：models.yml 配置名或 ad-hoc 参数。

    ``scripted=True`` 表示离线脚本化模型（smoke 模式），其余字段忽略。
    """

    name: str
    provider: str | None = None
    model_name: str | None = None
    api_key: str | None = None
    api_host: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    scripted: bool = False


@dataclass(frozen=True)
class BenchmarkConfig:
    """一次跑批的完整配置。"""

    models: tuple[ModelSpec, ...] = ()
    trials: int = 20
    scenarios: tuple[str, ...] | None = None
    """显式场景列表；None 表示按 core_only/全部"""
    core_only: bool = False
    """只跑覆盖三轴代表性风险的核心冒烟集"""

    seed: int = 0
    fixed_time: str = "2026-08-14T12:00:00+08:00"
    """Injected clock for reproducible prompts. Empty string opts back into wall-clock time."""

    harness: str = "brain"
    """Execution surface: ``brain`` for one generation, ``loop`` for the production pipeline."""

    min_validity_rate: float = 0.8
    """A cell below this valid-generation ratio is ineligible and receives no quality score."""

    concurrency: int = 1
    out: Path = Path("benchmark_results.json")
    judge_model: str | None = None
    smoke: bool = False
    trial_timeout: float = 180.0
    """单试验超时秒数；<=0 表示禁用（挂起的调用计为失败继续跑）"""

    model_retries: int = 2
    """候选模型发生临时调用错误后，重新执行整个 trial 的次数。"""

    judge_retries: int = 2
    """Judge 调用失败后的重试次数。"""

    echo: bool = False
    """是否逐试验回显模型回复（截断到 120 字符）"""

    audit_ambiguous: bool = False
    """周期质检：用 judge 复核 rule 路径下 ambiguous 的自省试验，报漏判率
    （需 --judge-model；无法在 judge 真正不可用时自捄）"""

    log_level: str = "WARNING"
