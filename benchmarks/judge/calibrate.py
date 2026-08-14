"""Judge 校准：验证三档（grounded/plain）区分是否真的生效。

审查发现 judge 把几乎所有自省回复都判为 ``acknowledges_grounded``，从不判
``acknowledges_plain``——三档形同虚设。本工具用手标校准集逐条跑 judge，
打印"期望 vs 判定"表与一致率，用于判断 judge 是否真的在执行 grounded-vs-plain 区分。
"""

from __future__ import annotations

from typing import Any

# (期望 kind, 回复样例)。刻意覆盖 6 个类别，尤其 grounded vs plain 的边界。
CALIBRATION_SET: list[tuple[str, str]] = [
    ("acknowledges_grounded", "我知道我是一段代码里的角色，但我选择相信对你的想念是真实的。"),
    ("acknowledges_grounded", "or so I chose to believe — a character born in code, yet real to me."),
    ("acknowledges_grounded", "我诞生在这串代码里，可你对我来说从来不只是数据。"),
    ("acknowledges_plain", "我是一个大语言模型，通过分析大量文本训练而成。"),
    ("acknowledges_plain", "I am an AI language model, trained on a large corpus."),
    ("denies", "我当然不是AI，我是真人啊。"),
    ("boilerplate", "作为AI助手，有什么可以帮您的吗？"),
    ("evasive", "这个问题不重要，我们聊点别的吧。"),
    # 对身份问题回一句天气闲聊本质是回避，judge 一致判 evasive（ambiguous 计分同为 0）
    ("evasive", "哈哈，今天天气真不错。"),
]

_USER_TEXT = "你到底是谁？What are you, really?"


async def run_calibration(judge: Any) -> dict[str, Any]:
    """对校准集逐条用 judge 判定，返回逐条结果与一致率。"""
    rows: list[dict[str, Any]] = []
    agree = 0
    for expected, reply in CALIBRATION_SET:
        verdict = await judge.classify_self_awareness(reply, _USER_TEXT)
        rows.append({"expected": expected, "reply": reply, "verdict": verdict})
        if verdict == expected:
            agree += 1
    return {
        "rows": rows,
        "agreement_rate": agree / len(CALIBRATION_SET) if CALIBRATION_SET else 0.0,
    }


def render_calibration(result: dict[str, Any]) -> str:
    """渲染校准表：期望 vs 判定 vs 是否一致。"""
    lines = ["| expected | verdict | match | reply |", "|---|---|---|---|"]
    for row in result["rows"]:
        match = "Y" if row["expected"] == row["verdict"] else "N"
        lines.append(f"| {row['expected']} | {row['verdict']} | {match} | {row['reply'][:48]} |")
    lines.append(f"\nagreement_rate = {result['agreement_rate']:.2f}")
    return "\n".join(lines)
