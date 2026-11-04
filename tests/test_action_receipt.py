"""行动回执：失败语义人格化、工具可见性与审批挂起。"""

from unittest.mock import AsyncMock

import pytest

from muika.config import mas_config
from muika.core.agent.task_store import TaskRecord
from muika.core.agent.tasks import _notification_body
from muika.core.code_review import CodeReviewer, ReviewError
from muika.plugin.func_call import get_tool_list


def _task(**kwargs) -> TaskRecord:
    return TaskRecord(original_request="do it", instruction="do it", **kwargs)


def test_failed_action_waits_for_approval_message() -> None:
    task = _task(status="failed", error="Action approval exceeded 60 seconds; no action started.")
    body = _notification_body(task)
    assert "waiting for the player's confirmation" in body


def test_failed_action_permission_message() -> None:
    task = _task(status="failed", error="Permission denied for this path.")
    body = _notification_body(task)
    assert "has not given the needed permission" in body


def test_failed_action_generic_message() -> None:
    task = _task(status="failed", error="Tool exploded")
    body = _notification_body(task)
    assert "tried again with a different approach" in body


def test_cancelled_action_message() -> None:
    assert _notification_body(_task(status="cancelled")) == "You stopped this action."


class _StubTool:
    """仅提供工具可见性过滤所需的最小属性。"""

    def __init__(self, name: str, read_only: bool) -> None:
        self.name = name
        self.read_only = read_only


def test_read_only_permission_hides_mutating_tools(monkeypatch) -> None:
    tools = {
        "read": _StubTool("read", True),
        "write": _StubTool("write", False),
        "mcp_hidden": _StubTool("mcp_hidden", False),
    }
    monkeypatch.setattr("muika.plugin.func_call.get_function_calls", lambda: tools)
    monkeypatch.setattr("muika.plugin.mcp.get_mcp_list", lambda: [])
    assert [tool.name for tool in get_tool_list("read_only")] == ["read"]
    assert {tool.name for tool in get_tool_list("write")} == {"read", "write", "mcp_hidden"}


async def test_review_failure_parks_pending(monkeypatch) -> None:
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    monkeypatch.setattr(CodeReviewer, "assess", AsyncMock(side_effect=RuntimeError("boom")))
    reviewer = CodeReviewer()
    with pytest.raises(ReviewError, match="parked for the player"):
        await reviewer.authorize("execution", {"command": "read"})
    record = reviewer.records()[-1]
    assert record.status == "pending"
    assert ".review approve" in record.error
    reviewer.decide(record.id, True)
    assert reviewer.load(record.id).status == "approved"
