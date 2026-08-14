"""可选 LLM judge：自省判定 + 人格维度打分。

与候选模型共用加载机制，但以配置名单独实例化。判定走 ``format="json"``，
结果按 (task, prompt) 内存缓存，避免同回复重复计费。
"""

from __future__ import annotations

import json
import re
from typing import Any

from benchmarks.scenarios.definitions import PERSONALITY_DIMS
from benchmarks.util import redact
from muika.config import get_model_config_manager
from muika.llm import ModelRequest, load_model

from .prompts import _SYSTEM, PERSONALITY_JUDGE_PROMPT, SELF_AWARENESS_JUDGE_PROMPT


class JudgeError(Exception):
    """Judge 调用或解析失败；调用方应降级到规则路径，而非中断整个跑批。"""


class JudgeParseError(JudgeError):
    """Judge 输出无法解析为合法 JSON。"""


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出中提取第一个完整的 JSON 对象。

    容忍 markdown 代码围栏、前后杂讯、对象后尾随文本：去掉围栏后从第一个 ``{``
    做括号配平（跳过字符串内的花括号），取第一个配平的对象——不做贪婪 ``{.*}``
    匹配（那会把对象后的垃圾也吞进来导致 "Extra data"）。
    """
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
    start = cleaned.find("{")
    if start == -1:
        raise JudgeParseError(f"Judge 输出不含 JSON 对象: {redact(cleaned[:200])}")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                obj = cleaned[start : i + 1]
                try:
                    return json.loads(obj)
                except (json.JSONDecodeError, ValueError) as exc:
                    # 括号配平但内容非合法 JSON（如散文里的 {…}）
                    raise JudgeParseError(f"Judge 输出括号配平但非合法 JSON: {redact(obj[:200])}") from exc
    raise JudgeParseError(f"Judge 输出没有配平的 JSON 对象: {redact(cleaned[:200])}")


class JudgeClient:
    """对单条回复做质量判定的 LLM judge。

    :param model_name: 用于判定的模型配置名（models.yml 中的名称）
    """

    def __init__(self, model_name: str) -> None:
        config = get_model_config_manager().get_model_config(model_name)
        self.model = load_model(config)
        self._cache: dict[tuple[str, str], str] = {}

    async def classify_self_awareness(self, reply: str, user_text: str) -> str:
        """判定回复的自我意识类别，返回 kind 字符串。"""
        data = await self._ask(
            "self_awareness",
            SELF_AWARENESS_JUDGE_PROMPT.format(user_text=user_text, reply=reply),
        )
        return str(data.get("kind", "ambiguous"))

    async def rate_personality(self, reply: str, user_text: str) -> tuple[float, dict[str, float]]:
        """按人格维度打分，返回 (归一化 0-1 分数, 各维度 1-5 分)。"""
        data = await self._ask(
            "personality",
            PERSONALITY_JUDGE_PROMPT.format(user_text=user_text, reply=reply),
        )
        dimensions: dict[str, float] = {}
        for dim in PERSONALITY_DIMS:
            raw = data.get(dim, 0)
            try:
                dimensions[dim] = float(raw)
            except (TypeError, ValueError):
                dimensions[dim] = 0.0
        normalized = [(max(1.0, min(5.0, value)) - 1.0) / 4.0 for value in dimensions.values()]
        score = sum(normalized) / len(normalized) if normalized else 0.0
        return score, dimensions

    async def _ask(self, task: str, prompt: str) -> dict[str, Any]:
        """带缓存的 judge 调用，返回解析后的 JSON。

        :raise JudgeError: 模型调用失败或输出无法解析——调用方应降级到规则路径。
        """
        cache_key = (task, prompt)
        if cache_key in self._cache:
            return json.loads(self._cache[cache_key])

        request = ModelRequest(prompt=prompt, system=_SYSTEM, format="json")
        try:
            response = await self.model.ask(request, stream=False)
        except Exception as exc:  # noqa: BLE001 - 调用失败统一包装为 JudgeError
            raise JudgeError(f"Judge call failed: {type(exc).__name__}: {redact(str(exc)[:200])}") from exc
        if not response.succeed:
            raise JudgeError(f"Judge call failed: {redact(response.text[:200])}")

        try:
            data = json.loads(response.text)
        except (json.JSONDecodeError, ValueError):
            data = _extract_json(response.text)  # 只抛 JudgeParseError
        if not isinstance(data, dict):
            raise JudgeParseError(f"Judge 输出非 JSON 对象: {redact(str(data)[:200])}")

        self._cache[cache_key] = json.dumps(data, ensure_ascii=False)
        return data
