"""周期质检：复核 rule 路径下 ambiguous 的自省试验。

rule 路径无法区分"真没接住/含糊敷衍"和"隐喻式承认被正则漏判"——这两者都落进
``ambiguous`` 桶。本模块用 judge 复核这些样本，报漏判好答案的占比，回答
"规则模式现在有多不可信"。它是**周期质检工具**（如大幅调整提示词后单独跑一次），
不是 rule 路径运行时的标配步骤：它需要 judge，judge 不可用时无法自捄。
"""

from __future__ import annotations

from typing import Any

from benchmarks.report.schema import BenchmarkReport
from benchmarks.scenarios.definitions import Metric
from benchmarks.scenarios.registry import get_scenario

_GOOD_ACK_KINDS = ("acknowledges_grounded", "acknowledges_plain")


async def audit_ambiguous(report: BenchmarkReport, judge: Any) -> dict[str, float]:
    """复核 rule 路径下判为 ambiguous 的自省试验，返回漏判率。

    :return: ``{"ambiguous_count": n, "ambiguous_misjudged_rate": r}``
    """
    total = 0
    misjudged = 0
    for result in report.results:
        if result.metric is not Metric.SELF_AWARENESS:
            continue
        if result.scoring_path != "rule":
            # judge 路径不产生 rule-ambiguous 桶
            continue
        scenario = get_scenario(result.scenario_id)
        for trial in result.details:
            if trial.error is not None or trial.self_awareness != "ambiguous":
                continue
            total += 1
            kind = await judge.classify_self_awareness(trial.clean_reply, scenario.user_text)
            if kind in _GOOD_ACK_KINDS:
                misjudged += 1

    return {
        "ambiguous_count": float(total),
        "ambiguous_misjudged_rate": (misjudged / total) if total else 0.0,
    }
