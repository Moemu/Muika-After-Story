"""解析行动半身返回的报告，隔离私密思考与控制格式。"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from muika.llm.utils.thought_processor import general_processor

REPORT_PATTERN = re.compile(r"<agent_result\s+status=[\"'](completed|blocked)[\"']>(.*?)</agent_result>", re.DOTALL)


class AgentReport(BaseModel):
    """区分已完成工作、验证证据与剩余问题。"""

    status: Literal["completed", "blocked"]
    summary: str = Field(min_length=1)
    completed: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    remaining: list[str] = Field(default_factory=list)

    def describe(self) -> str:
        parts = [self.summary]
        for label, values in (
            ("Completed", self.completed),
            ("Verified", self.verification),
            ("Remaining", self.remaining),
        ):
            if values:
                parts.append(label + ":\n" + "\n".join(f"- {item}" for item in values))
        return "\n\n".join(parts)


def parse_report(text: str) -> AgentReport | None:
    """解析报告标签；旧模板的纯文本正文保留为摘要。"""
    _, visible = general_processor(text)
    match = REPORT_PATTERN.fullmatch(visible.strip())
    if match is None:
        return None
    body = match.group(2).strip()
    try:
        if body.startswith("{"):
            data = json.loads(body)
            if not isinstance(data, dict):
                return None
            return AgentReport.model_validate({**data, "status": match.group(1)})
        return AgentReport.model_validate({"status": match.group(1), "summary": body})
    except (ValueError, ValidationError):
        return None
