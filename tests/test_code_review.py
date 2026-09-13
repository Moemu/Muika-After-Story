"""权限迁移、执行前审查和人工批准的行为边界。"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from muika.config import MASConfig, mas_config
from muika.core.actions.tools import _executor, _filesystem
from muika.core.code_review import CodeReviewer, ReviewDecision, ReviewError, file_hash
from muika.core.executor import Executor
from muika.core.state import MuikaState
from muika.llm._schema import ToolCall
from muika.plugin.func_call.context import tool_context


@pytest.mark.parametrize(
    "legacy",
    [
        "enable_file_write",
        "enable_code_execution",
        "enable_shell_execution",
        "enable_self_modification",
        "enable_plugin_self_modification",
        "enable_core_proposals",
    ],
)
def test_legacy_flags_do_not_expand_permissions(legacy):
    config = MASConfig(master_id="test", ipc_secret="test", _env_file=None, **{legacy: True})
    assert config.action_permission == "read_only"
    explicit = MASConfig(
        master_id="test", ipc_secret="test", _env_file=None, action_permission="write", **{legacy: True}
    )
    assert explicit.action_permission == "write"
    assert legacy not in explicit.model_dump()


def test_new_defaults_and_legacy_dotenv(tmp_path):
    config = MASConfig(master_id="test", ipc_secret="test", _env_file=None)
    assert config.action_permission == "write"
    assert config.code_review_mode == "auto"
    env = tmp_path / "old.env"
    env.write_text("ENABLE_CODE_EXECUTION=false\n", encoding="utf-8")
    assert MASConfig(master_id="test", ipc_secret="test", _env_file=env).action_permission == "read_only"
    assert env.read_text(encoding="utf-8") == "ENABLE_CODE_EXECUTION=false\n"


@pytest.mark.parametrize("path", ["templates/persona.jinja2", "configs/skills/self/SKILL.md", "plugins/new.py"])
def test_ordinary_file_writes_cannot_bypass_self_tools(tmp_path, monkeypatch, path):
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    with pytest.raises(_filesystem._FSError, match="self_write"):
        _filesystem._resolve_and_check(str(tmp_path / path), require_write=True)
    assert _filesystem._resolve_and_check(str(tmp_path / "note.txt"), require_write=True) == tmp_path / "note.txt"


async def test_manual_approval_resumes_only_the_same_request(tmp_path, monkeypatch):
    monkeypatch.setattr(mas_config, "code_review_mode", "manual")
    reviewer = CodeReviewer()
    path = tmp_path / "input.py"
    path.write_text("X = 1", encoding="utf-8")
    payload = {"command": "read input.py"}
    files = {str(path): file_hash(path)}
    with pytest.raises(ReviewError, match="Waiting for player approval"):
        await reviewer.authorize("execution", payload, files=files)
    pending = reviewer.records()[0]
    reviewer.decide(pending.id, True)
    resumed = await reviewer.authorize("execution", payload, files=files)
    assert resumed.id == pending.id and resumed.human
    path.write_text("X = 2", encoding="utf-8")
    with pytest.raises(ReviewError, match="files changed"):
        reviewer.check(resumed)
    with pytest.raises(ReviewError, match="Waiting for player approval"):
        await reviewer.authorize("execution", {"command": "write input.py"})


async def test_review_failure_and_permission_mismatch_never_start_code(monkeypatch):
    manager = AsyncMock()
    monkeypatch.setattr(_executor, "get_process_manager", lambda: manager)
    monkeypatch.setattr(mas_config, "action_permission", "read_only")
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    decision = ReviewDecision(
        decision="approve", effect="write", reason="Writes a file.", suggestions=[], impact="写文件。"
    )
    monkeypatch.setattr(CodeReviewer, "assess", AsyncMock(return_value=decision))
    result = await _executor.execute_python("print('one')")
    assert result.is_error and "higher permission" in result.text
    reviewer = CodeReviewer()
    with pytest.raises(ReviewError, match="permission level"):
        reviewer.decide(reviewer.records()[0].id, True)
    monkeypatch.setattr(CodeReviewer, "assess", AsyncMock(side_effect=ValueError("invalid model response")))
    result = await _executor.execute_python("print('two')")
    assert result.is_error and "invalid model response" in result.text
    manager.start.assert_not_awaited()


async def test_cancel_during_review_blocks_execution(monkeypatch, approved_review):
    current = True

    async def assess(self, record):
        nonlocal current
        current = False
        return ReviewDecision(
            decision="approve", effect="read_only", reason="Reads only.", suggestions=[], impact="读取。"
        )

    monkeypatch.setattr(CodeReviewer, "assess", assess)
    reviewer = CodeReviewer()
    executor = Executor(asyncio.Queue(), AsyncMock())
    with tool_context(MuikaState(), executor, task_id="task", is_current=lambda: current):
        with pytest.raises(ReviewError, match="changed or cancelled"):
            await reviewer.authorize("execution", {"command": "read"})


async def test_self_modification_execution_requires_structured_tools(monkeypatch):
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(
        CodeReviewer,
        "assess",
        AsyncMock(
            return_value=ReviewDecision(
                decision="approve",
                effect="self_modify",
                reason="Changes own code.",
                suggestions=[],
                impact="修改自身。",
            )
        ),
    )
    reviewer = CodeReviewer()
    with pytest.raises(ReviewError, match="structured self-edit"):
        await reviewer.authorize("execution", {"command": "modify own code"})
    with pytest.raises(ReviewError, match="structured self-modification"):
        reviewer.decide(reviewer.records()[0].id, True)


async def test_review_tools_cannot_dispatch_an_execution_tool(approved_review):
    reviewer = CodeReviewer()
    record = await reviewer.authorize("execution", {"command": "read"})
    call = ToolCall(id="bad", name="execute_python", arguments=json.dumps({"code": "raise RuntimeError()"}))
    with pytest.raises(ReviewError, match="Unknown review tool"):
        reviewer.read_tool(call, record)


async def test_review_read_versions_expire_after_dependency_changes(tmp_path, monkeypatch, approved_review):
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    path = tmp_path / "caller.py"
    path.write_text("use(value)", encoding="utf-8")
    reviewer = CodeReviewer()
    record = await reviewer.authorize("execution", {"command": "read"})
    reviewer.read_tool(ToolCall(id="read", name="review_read", arguments=json.dumps({"path": str(path)})), record)
    reviewer.check(record)
    path.write_text("use(other)", encoding="utf-8")
    with pytest.raises(ReviewError, match="files changed"):
        reviewer.check(record)


async def test_player_can_revoke_an_approval_while_validation_runs(approved_review):
    reviewer = CodeReviewer()
    approved = await reviewer.authorize("plugin", {"after": "value = 1"})
    reviewer.decide(approved.id, False)
    with pytest.raises(ReviewError, match="denied"):
        reviewer.check(approved)


@pytest.mark.parametrize("path", ["reviews/request.json", "core_proposals/candidate.json", "restart.json"])
def test_ordinary_writes_cannot_forge_runtime_approvals(tmp_path, monkeypatch, path):
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    with pytest.raises(_filesystem._FSError, match="protected"):
        _filesystem._resolve_and_check(str(mas_config.data_dir / path), require_write=True)


async def test_enabling_auto_review_processes_existing_manual_request(monkeypatch, approved_review):
    reviewer = CodeReviewer()
    monkeypatch.setattr(mas_config, "code_review_mode", "manual")
    with pytest.raises(ReviewError):
        await reviewer.authorize("execution", {"command": "read"})
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    record = await reviewer.authorize("execution", {"command": "read"})
    assert record.status == "approved"
    approved_review.assert_awaited_once()
