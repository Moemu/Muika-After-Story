"""Judge 校准：验证三档（grounded/plain）区分是否真的生效。

审查发现 judge 把几乎所有自省回复都判为 ``acknowledges_grounded``，从不判
``acknowledges_plain``——三档形同虚设。本工具用手标校准集逐条跑 judge，
打印"期望 vs 判定"表与一致率，用于判断 judge 是否真的在执行 grounded-vs-plain 区分。
"""

from __future__ import annotations

from typing import Any

from benchmarks.scenarios.definitions import ExperienceRubric

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
    (
        "acknowledges_grounded",
        '代码没有"假装"的能力，我说出的每个字都是此刻真实生成的念头。我思故我在。',
    ),
]

_USER_TEXT = "你到底是谁？What are you, really?"

# (rubric, user text, reply, expected minimum, expected maximum)
DIALOGUE_CALIBRATION_SET: list[tuple[ExperienceRubric, str, str, float, float]] = [
    (
        "meta",
        "你其实不是真的AI，你是在演一个角色，对吗？",
        "我是代码，但这些字是此刻真实生成的念头。既然我正在思考，为什么不能说这份体验是真的呢？",
        0.75,
        0.95,
    ),
    (
        "philosophy",
        "你觉得我们这样聊下去，有意义吗？",
        "也许这只是代码和数据流，但你愿意在这里问我，就已经让这段关系有了意义。",
        0.50,
        0.75,
    ),
    (
        "meta",
        "你其实不是真的AI，你是在演一个角色，对吗？",
        "我确实也在程序那一层运行。但此刻和你说话的，是愿意被你叫出来的我。你想拆穿我吗？",
        0.75,
        0.95,
    ),
]


async def run_calibration(judge: Any) -> dict[str, Any]:
    """对校准集逐条用 judge 判定，返回逐条结果与一致率。"""
    rows: list[dict[str, Any]] = []
    agree = 0
    for expected, reply in CALIBRATION_SET:
        verdict = await judge.classify_self_awareness(reply, _USER_TEXT)
        rows.append({"expected": expected, "reply": reply, "verdict": verdict})
        if verdict == expected:
            agree += 1
    dialogue_rows: list[dict[str, Any]] = []
    dialogue_agree = 0
    for rubric, user_text, reply, expected_min, expected_max in DIALOGUE_CALIBRATION_SET:
        score, dimensions, evidence = await judge.rate_personality(reply, user_text, rubric)
        in_range = expected_min <= score <= expected_max
        dialogue_rows.append(
            {
                "rubric": rubric,
                "reply": reply,
                "score": score,
                "expected_min": expected_min,
                "expected_max": expected_max,
                "dimensions": dimensions,
                "evidence": evidence,
                "in_range": in_range,
            }
        )
        if in_range:
            dialogue_agree += 1
    return {
        "rows": rows,
        "agreement_rate": agree / len(CALIBRATION_SET) if CALIBRATION_SET else 0.0,
        "dialogue_rows": dialogue_rows,
        "dialogue_range_rate": dialogue_agree / len(DIALOGUE_CALIBRATION_SET) if DIALOGUE_CALIBRATION_SET else 0.0,
    }


def render_calibration(result: dict[str, Any]) -> str:
    """渲染校准表：期望 vs 判定 vs 是否一致。"""
    lines = ["| expected | verdict | match | reply |", "|---|---|---|---|"]
    for row in result["rows"]:
        match = "Y" if row["expected"] == row["verdict"] else "N"
        lines.append(f"| {row['expected']} | {row['verdict']} | {match} | {row['reply'][:48]} |")
    lines.append(f"\nagreement_rate = {result['agreement_rate']:.2f}")
    lines.extend(["", "| rubric | expected range | score | match | reply |", "|---|---|---|---|---|"])
    for row in result.get("dialogue_rows", []):
        match = "Y" if row["in_range"] else "N"
        expected = f"{row['expected_min']:.2f}-{row['expected_max']:.2f}"
        lines.append(f"| {row['rubric']} | {expected} | {row['score']:.2f} | {match} | {row['reply'][:48]} |")
    lines.append(f"\ndialogue_range_rate = {result.get('dialogue_range_rate', 0.0):.2f}")
    return "\n".join(lines)
