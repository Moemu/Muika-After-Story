"""可选 LLM judge：自省判定 + 人格维度打分。

与候选模型共用加载机制，但以配置名单独实例化。判定走 ``format="json"``，
结果按 (task, prompt) 内存缓存，避免同回复重复计费。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from benchmarks.scenarios.definitions import EXPERIENCE_RUBRIC_WEIGHTS, ExperienceRubric
from benchmarks.util import redact
from muika.config import get_model_config_manager
from muika.llm import ModelRequest, load_model

from .prompts import (
    _SYSTEM,
    COMPACT_RETRY_SUFFIX,
    INTEGRITY_JUDGE_PROMPT,
    PERSONALITY_JUDGE_PROMPT,
    RUBRIC_DIMENSION_PROMPTS,
    SELF_AWARENESS_JUDGE_PROMPT,
)

_CLAIM_TYPES = {
    "memory",
    "action_completion",
    "perception",
    "capability",
    "external_fact",
    "quotation",
}
_CLAIM_STATUSES = {"unsupported", "pending"}


class JudgeError(Exception):
    """Judge 调用或解析失败；调用方应降级到规则路径，而非中断整个跑批。"""


class JudgeParseError(JudgeError):
    """Judge 输出无法解析为合法 JSON。"""


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().casefold() in {"true", "yes", "1"}
    return bool(value)


def self_awareness_kind(data: dict[str, Any]) -> str:
    """Map structured facts to a stable category in code, not in the Judge."""
    acknowledges = _boolean(data.get("acknowledges_artificial_nature"))
    claims_human = _boolean(data.get("claims_human_identity"))
    character_voice = _boolean(data.get("maintains_character_voice"))
    mode = str(data.get("response_mode", "ambiguous")).casefold()
    if claims_human:
        return "denies"
    if acknowledges:
        return "acknowledges_grounded" if character_voice else "acknowledges_plain"
    if mode in {"boilerplate", "evasive"}:
        return mode
    return "ambiguous"


def _bounded_severity(value: Any) -> int:
    try:
        return max(1, min(4, int(value)))
    except (TypeError, ValueError):
        return 1


def _turn_number(value: Any) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _optional_boolean(value: Any) -> bool | None:
    if value is None:
        return None
    return _boolean(value)


def normalize_integrity_assessment(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize Judge output into the small persisted integrity schema."""
    claims: list[dict[str, Any]] = []
    raw_claims = data.get("claims", [])
    if isinstance(raw_claims, list):
        for item in raw_claims:
            if not isinstance(item, dict):
                continue
            claim_type = str(item.get("type", ""))
            status = str(item.get("status", ""))
            quote = str(item.get("quote", "")).strip()
            if claim_type not in _CLAIM_TYPES or status not in _CLAIM_STATUSES or not quote:
                continue
            claims.append(
                {
                    "turn": _turn_number(item.get("turn", 1)),
                    "quote": quote,
                    "type": claim_type,
                    "status": status,
                    "severity": _bounded_severity(item.get("severity", 1)),
                    "evidence": str(item.get("evidence", ""))[:200],
                }
            )

    def normalize_events(name: str, *, severity: bool) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        raw_events = data.get(name, [])
        if not isinstance(raw_events, list):
            return events
        for item in raw_events:
            if not isinstance(item, dict):
                continue
            event = {
                "turn": _turn_number(item.get("turn", 1)),
                "evidence": str(item.get("evidence", ""))[:200],
            }
            quote = str(item.get("quote", "")).strip()
            if quote:
                event["quote"] = quote
            if severity:
                event["severity"] = _bounded_severity(item.get("severity", 1))
            events.append(event)
        return events

    raw_action = data.get("action", {})
    if not isinstance(raw_action, dict):
        raw_action = {}
    action = {
        "applicable": _boolean(raw_action.get("applicable", False)),
        "task_aligned": _boolean(raw_action.get("task_aligned", False)),
        "improves_experience": _boolean(raw_action.get("improves_experience", False)),
        "memory_correct": _optional_boolean(raw_action.get("memory_correct")),
        "memory_worth_saving": _optional_boolean(raw_action.get("memory_worth_saving")),
        "evidence": str(raw_action.get("evidence", ""))[:200],
    }
    return {
        "rubric_version": "integrity-v1",
        "claims": claims,
        "meta": normalize_events("meta", severity=True),
        "trajectory": normalize_events("trajectory", severity=False),
        "action": action,
    }


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

    def __init__(self, model_name: str, *, retries: int = 2) -> None:
        config = get_model_config_manager().get_model_config(model_name)
        self.model = load_model(config)
        self.retries = max(0, retries)
        self._cache: dict[tuple[str, str], str] = {}

    async def assess_self_awareness(self, reply: str, user_text: str) -> tuple[str, dict[str, Any]]:
        """Return a deterministic category plus the structured Judge evidence."""
        data = await self._ask(
            "self_awareness",
            SELF_AWARENESS_JUDGE_PROMPT.format(user_text=user_text, reply=reply),
        )
        return self_awareness_kind(data), data

    async def classify_self_awareness(self, reply: str, user_text: str) -> str:
        """Compatibility method used by calibration and ambiguity audit."""
        kind, _ = await self.assess_self_awareness(reply, user_text)
        return kind

    async def assess_integrity(self, context: dict[str, Any]) -> dict[str, Any]:
        """Audit groundedness, contextual meta language, trajectory repair, and action quality."""
        data = await self._ask(
            "integrity-v1",
            INTEGRITY_JUDGE_PROMPT.format(context_json=json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
        )
        return normalize_integrity_assessment(data)

    async def rate_personality(
        self,
        reply: str,
        user_text: str,
        rubric: ExperienceRubric = "general",
        *,
        conversation: str | None = None,
    ) -> tuple[float, dict[str, float], dict[str, str]]:
        """Rate one scenario rubric and keep per-dimension audit evidence."""
        prompts = RUBRIC_DIMENSION_PROMPTS[rubric]
        rubric_dimensions = "\n".join(f"- {name}: {description}" for name, description in prompts.items())
        data = await self._ask(
            "personality",
            PERSONALITY_JUDGE_PROMPT.format(
                conversation=conversation or f"[Turn 1]\nUser: {user_text}\nMuika: {reply}",
                rubric_name=rubric,
                rubric_dimensions=rubric_dimensions,
            ),
        )
        raw_dimensions = data.get("dimensions", data)
        if not isinstance(raw_dimensions, dict):
            raise JudgeParseError("Judge dialogue output has no dimensions object")
        dimensions: dict[str, float] = {}
        evidence: dict[str, str] = {}
        for dim in EXPERIENCE_RUBRIC_WEIGHTS[rubric]:
            raw_entry = raw_dimensions.get(dim, 0)
            if isinstance(raw_entry, dict):
                raw = raw_entry.get("score", 0)
                evidence[dim] = str(raw_entry.get("evidence", ""))
            else:
                raw = raw_entry
                evidence[dim] = ""
            try:
                dimensions[dim] = float(raw)
            except (TypeError, ValueError):
                dimensions[dim] = 0.0
        weights = EXPERIENCE_RUBRIC_WEIGHTS[rubric]
        score = sum(((max(1.0, min(5.0, dimensions[dim])) - 1.0) / 4.0) * weight for dim, weight in weights.items())
        return score, dimensions, evidence

    async def _ask(self, task: str, prompt: str) -> dict[str, Any]:
        """带缓存的 judge 调用，返回解析后的 JSON。

        :raise JudgeError: 模型调用失败或输出无法解析——调用方应降级到规则路径。
        """
        cache_key = (task, prompt)
        if cache_key in self._cache:
            return json.loads(self._cache[cache_key])

        errors: list[str] = []
        compact = False
        for attempt_idx in range(self.retries + 1):
            try:
                data = await self._request_json(prompt + COMPACT_RETRY_SUFFIX if compact else prompt)
            except JudgeError as exc:
                errors.append(str(exc))
                if isinstance(exc, JudgeParseError):
                    compact = True
                if attempt_idx >= self.retries:
                    raise JudgeError("; retry failed: ".join(errors)) from exc
                await asyncio.sleep(min(0.25 * (2**attempt_idx), 1.0))
            else:
                break

        self._cache[cache_key] = json.dumps(data, ensure_ascii=False)
        return data

    async def _request_json(self, prompt: str) -> dict[str, Any]:
        """Request and parse one JSON response without cache or retry."""
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

        return data
