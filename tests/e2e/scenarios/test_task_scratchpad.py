"""任务沙箱：data 根防护拦截散落写入、重复失败触发熔断引导、执行默认落在任务 scratch。"""

import asyncio
import json
from pathlib import Path
from time import perf_counter
from unittest.mock import AsyncMock

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.core import code_review
from muika.core.code_review import CodeReviewer, ReviewError, ReviewRecord
from muika.core.self_mod import proposals
from muika.core.state import MuikaState
from muika.llm import ModelRequest
from muika.llm._execution import dispatch_call
from muika.llm._schema import ToolCall
from muika.llm.context import strip_json_fence
from muika.plugin.func_call import get_tool_list
from muika.plugin.func_call.context import tool_context
from tests.e2e.scenarios.test_command_dispatch import builtin_plugins

pytestmark = pytest.mark.e2e


@pytest.mark.usefixtures(builtin_plugins.__name__)
@pytest.mark.parametrize("decision", ["approve", "deny", "unavailable", "stale", "racing"])
async def test_approval_blocks_batch_and_records_player_decision(
    core_app_factory, recorder, monkeypatch, tmp_path, decision
):
    """批准阻塞保存未执行调用，玩家决定只恢复对应任务。"""
    monkeypatch.setattr(mas_config, "code_review_mode", "manual" if decision != "unavailable" else "auto")
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    marker = tmp_path / "executed.txt"
    before = tmp_path / "before.txt"
    after = tmp_path / "after.txt"
    execute = ToolCall(
        id="execute",
        name="execute_python",
        arguments=json.dumps(
            {
                "code": (
                    f"from pathlib import Path; p=Path({str(marker)!r}); "
                    "p.write_text(p.read_text()+'x' if p.exists() else 'x')"
                )
            }
        ),
    )
    turns = [
        ScriptedTurn(
            when=lambda req: "[User]" in req.prompt,
            name="delegate",
            text="我想做个小实验。<agent>记录实验结果。</agent>",
        ),
        ScriptedTurn(
            when=_is_agent_request,
            name="batch",
            text="",
            tool_calls=[
                ToolCall(
                    id="before", name="write_file", arguments=json.dumps({"path": str(before), "content": "saved"})
                ),
                execute,
                ToolCall(id="after", name="write_file", arguments=json.dumps({"path": str(after), "content": "later"})),
            ],
        ),
        ScriptedTurn(when=lambda req: "[Action result]" in req.prompt, name="blocked", text="实验先停在这里。"),
    ]
    if decision in {"approve", "racing"}:
        turns.extend(
            [
                ScriptedTurn(
                    when=_is_agent_request,
                    name="resume",
                    text="",
                    tool_calls=[execute.model_copy(update={"id": "resume-execute"})],
                ),
                ScriptedTurn(
                    when=_is_agent_request, name="report", text=_report("Experiment completed.", "One write.")
                ),
                ScriptedTurn(when=lambda req: "[Action result]" in req.prompt, name="completed", text="实验完成了。"),
            ]
        )
    elif decision == "deny":
        turns.append(
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="denied",
                text="好，这次实验就停下，结果还没有写入。",
            )
        )
    if decision == "racing":
        turns = [turn for turn in turns if turn.name != "blocked"]
    app = await core_app_factory(turns=turns)
    if decision == "unavailable":
        monkeypatch.setattr(CodeReviewer, "assess", AsyncMock(side_effect=ReviewError("Service unavailable")))
    await app.start()
    if decision == "racing":
        monkeypatch.setattr(app.muika.agent_tasks, "_remember_call", _slow_approval)
    await app.user_says("继续你的实验吧。")
    await app.next_reply()
    assert app.muika is not None
    if decision == "racing":
        for _ in range(500):
            task = next(iter(app.muika.agent_tasks.tasks.values()), None)
            if task and task.pending_review_id:
                break
            await asyncio.sleep(0.01)
        assert task and task.pending_review_id
        await app.say_command(f".review approve {task.pending_review_id}")
        await app.next_reply(timeout=20)
        await app.wait_processed("agent_task")
        assert task.status == "completed" and marker.read_text() == "x" and not after.exists()
        calls = await app.muika.agent_tasks.store.calls(task.id)
        assert any(call.call.id == "after" and "Not executed" in call.result.text for call in calls)
        assert app.scripted.pending_turns == 0
        recorder.record("approval_race", skipped=1, executed=1)
        return
    await app.next_reply(timeout=20)
    await app.wait_processed("agent_task")
    task = next(iter(app.muika.agent_tasks.tasks.values()))
    assert task.status == "blocked" and task.pending_review_id
    review_id = task.pending_review_id
    calls = await app.muika.agent_tasks.store.calls(task.id)
    assert len(calls) == 3
    assert all(call.status == "completed" for call in calls)
    results = {call.call.id: call.result for call in calls}
    assert results["execute"].review_id == review_id
    assert "Not executed" in results["after"].text
    assert before.read_text() == "saved" and not after.exists() and not marker.exists()
    if decision == "stale":
        await app.muika.agent_tasks.update(task.id, "Stop this experiment.", cancel=True)
        assert task.pending_review_id is None
        await app.say_command(f".review approve {review_id}")
        assert task.status == "cancelled" and not marker.exists()
    elif decision != "unavailable":
        await app.say_command(f".review {decision} {review_id}")
        await app.next_reply(timeout=20)
        await app.wait_processed("agent_task")
        assert task.pending_review_id is None
        assert task.status == ("completed" if decision == "approve" else "blocked")
        assert marker.exists() == (decision == "approve")
        if marker.exists():
            assert marker.read_text() == "x"
    assert app.scripted.pending_turns == 0
    recorder.record(
        "approval_batch", decision=decision, task_status=task.status, review_id=review_id, executed=marker.exists()
    )


@pytest.mark.parametrize(
    "outputs, succeeds",
    [
        (["说明\n```json\n{body}\n```\n结束"], True),
        (["{body} trailing explanation"], True),
        (["invalid", "{body}"], True),
        (["invalid", "invalid"], False),
    ],
)
async def test_approval_has_no_tools_and_repairs_format_once(
    core_app_factory, recorder, monkeypatch, outputs, succeeds
):
    """完整批准无工具，格式修复最多请求一次。"""
    body = json.dumps(
        {
            "decision": "approve",
            "effect": "read_only",
            "reason": "Calculates only.",
            "suggestions": [],
            "impact": "计算。",
        }
    )
    app = await core_app_factory(
        turns=[
            ScriptedTurn(when=lambda req: not req.tools, name=f"approval-{index}", text=output.replace("{body}", body))
            for index, output in enumerate(outputs)
        ]
    )
    monkeypatch.setattr(code_review, "load_model", lambda config: app.scripted)
    monkeypatch.setattr(code_review, "get_model_config", lambda name: app.scripted.config)
    record = ReviewRecord(
        id="0" * 64,
        kind="execution",
        payload={"command": ["python", "-c", "print(1)"]},
        owner=None,
        context="Muika's experiment",
        permission="write",
        allowed_paths=[],
    )
    if succeeds:
        assert (await CodeReviewer().assess(record)).decision == "approve"
    else:
        with pytest.raises(ValueError):
            await CodeReviewer().assess(record)
    assert len(app.scripted.calls) == len(outputs)
    assert all(not call["tool_calls"] for call in app.scripted.calls)
    assert strip_json_fence(f"Before {body} after") == body
    recorder.record("approval_format", attempts=len(outputs), succeeded=succeeds)


async def _slow_approval(record):
    """模拟超过批准截止时间的响应。"""
    await asyncio.sleep(1)


@pytest.mark.parametrize("variant", ["forward", "escaped"])
async def test_script_evidence_uses_path_variants(core_app_factory, recorder, monkeypatch, tmp_path, variant):
    """不同路径写法都绑定已读脚本，无关文件不绑定，改脚本使批准失效。"""
    monkeypatch.setattr(mas_config, "code_review_mode", "manual")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    script = tmp_path / "job.py"
    other = tmp_path / "other.txt"
    script.write_text("print('original')", encoding="utf-8")
    other.write_text("old", encoding="utf-8")
    app = await core_app_factory()
    tools = {tool.name: tool for tool in get_tool_list()}
    with tool_context(MuikaState(), app.executor, task_id="script-evidence"):
        for path in (script, other):
            result = await dispatch_call(
                ToolCall(id=path.name, name="read_file", arguments=json.dumps({"path": str(path)})), tools
            )
            assert not result.is_error
        spelling = script.as_posix() if variant == "forward" else str(script)
        result = await dispatch_call(
            ToolCall(
                id="execute", name="execute_python", arguments=json.dumps({"code": f"exec(open({spelling!r}).read())"})
            ),
            tools,
        )
        assert result.is_error and result.review_id
        review = CodeReviewer().load(result.review_id)
        assert review.files == {str(script): code_review.file_hash(script)}
        assert review.payload["sources"] == {str(script): "print('original')"}
        other.write_text("changed", encoding="utf-8")
        CodeReviewer().decide(review.id, True)
        script.write_text("print('changed')", encoding="utf-8")
        with pytest.raises(ReviewError, match="files changed"):
            CodeReviewer().check(CodeReviewer().load(review.id))
    recorder.record("script_evidence", variant=variant, bound_files=1, stale_blocked=True)


@pytest.mark.usefixtures(builtin_plugins.__name__)
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_handoff_approval_preserves_execution_owner(core_app_factory, recorder, monkeypatch, tmp_path, decision):
    """主人格审批保存关联，批准保留执行权，拒绝不会恢复后台动作。"""
    monkeypatch.setattr(mas_config, "code_review_mode", "manual")
    monkeypatch.setattr(mas_config, "action_permission", "write")
    marker = tmp_path / "handoff.txt"
    app = await core_app_factory(
        turns=(
            [
                ScriptedTurn(
                    when=lambda req: "[Action result]" in req.prompt,
                    name="denied_handoff",
                    text="这次动作停下了，我还在这里。",
                )
            ]
            if decision == "deny"
            else []
        )
    )
    await app.start()
    assert app.muika is not None
    manager = app.muika.agent_tasks
    task = await manager.submit("Write the experiment result.", "Try the experiment.")
    await manager.handoff()
    call = ToolCall(
        id="handoff",
        name="execute_python",
        arguments=json.dumps({"code": f"from pathlib import Path; Path({str(marker)!r}).write_text('done')"}),
    )
    result = await manager.execute_persona_call(call)
    assert result.review_id and task.pending_review_id == result.review_id
    await app.say_command(f".review {decision} {result.review_id}")
    assert task.handoff and task.status == "blocked" and task.pending_review_id is None
    if decision == "approve":
        result = await manager.execute_persona_call(call.model_copy(update={"id": "resumed-handoff"}))
        assert not result.is_error and marker.read_text() == "done"
        await manager.complete_handoff(task.id, _report("Experiment completed.", "Read the result."))
        assert task.status == "completed"
    else:
        await manager.release_persona()
        assert task.status == "blocked" and not marker.exists()
        await app.next_reply()
        assert app.scripted.pending_turns == 0
    recorder.record("handoff_approval", decision=decision, task_status=task.status, executed=marker.exists())


@pytest.mark.parametrize("kind", ["plugin", "core"])
async def test_self_mod_approval_survives_error_conversion(core_app_factory, recorder, monkeypatch, tmp_path, kind):
    """插件和 Core 的异常转换保留批准 ID，候选失败不修改正式文件。"""
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "plugins_dir", "plugins")
    (tmp_path / "plugins").mkdir()
    (tmp_path / "muika").mkdir()
    monkeypatch.setattr(proposals, "_manager", proposals.CoreProposalManager(tmp_path))
    monkeypatch.setattr(CodeReviewer, "assess", AsyncMock(side_effect=ReviewError("Approval service unavailable")))
    app = await core_app_factory()
    await app.start()
    assert app.muika is not None
    if kind == "plugin":
        call = ToolCall(
            id="plugin",
            name="self_write",
            arguments=json.dumps(
                {
                    "path": "plugins/toy.py",
                    "content": (
                        "from muika.plugin.models import PluginMetadata\n" "metadata = PluginMetadata(name='toy')\n"
                    ),
                    "reason": "Try a new interest.",
                }
            ),
        )
        target = tmp_path / "plugins/toy.py"
    else:
        call = ToolCall(
            id="core",
            name="propose_core_change",
            arguments=json.dumps(
                {
                    "changes": [{"action": "create", "path": "muika/toy.py", "content": "VALUE = 1\n"}],
                    "reason": "Try a small change.",
                }
            ),
        )
        target = tmp_path / "muika/toy.py"
    with tool_context(app.muika.state, app.executor, task_id=f"{kind}-approval"):
        result = await dispatch_call(call, {tool.name: tool for tool in get_tool_list()})
    assert result.is_error and result.review_id
    record = CodeReviewer().load(result.review_id)
    assert record.kind == kind and record.status == "unavailable"
    assert not target.exists()
    recorder.record("self_mod_approval", action_kind=kind, review_id=record.id, formal_file_changed=False)


@pytest.mark.parametrize("waiting", ["lock", "model"])
async def test_approval_deadline_preserves_request(core_app_factory, recorder, monkeypatch, waiting):
    """排队和模型等待到期均保存不可用请求，迟到结果不获批准。"""
    app = await core_app_factory()
    limit = 0.1
    monkeypatch.setattr(code_review, "REVIEW_TIMEOUT_SECONDS", limit)
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    reviewer = CodeReviewer()
    gate = asyncio.Lock()
    monkeypatch.setattr(code_review, "_LOCK", gate)
    if waiting == "lock":
        await gate.acquire()
    else:
        monkeypatch.setattr(reviewer, "assess", _slow_approval)
    started = perf_counter()
    try:
        with tool_context(MuikaState(), app.executor) as context:
            with pytest.raises(ReviewError):
                await reviewer.authorize("execution", {"command": "print(1)"})
            assert context.review_id
            record = reviewer.load(context.review_id)
            assert record.status == "unavailable"
    finally:
        if gate.locked():
            gate.release()
    elapsed = perf_counter() - started
    assert elapsed < limit + 0.2
    recorder.record("approval_deadline", waiting=waiting, seconds=elapsed, status=record.status)


def _is_agent_request(request: ModelRequest) -> bool:
    """Agent 半身的请求带工具清单；主人格（非上帝模式）不带。"""
    return bool(request.tools)


def _report(summary: str, verification: str) -> str:
    return (
        '<agent_result status="completed">'
        + json.dumps({"summary": summary, "verification": [verification]}, ensure_ascii=False)
        + "</agent_result>"
    )


@pytest.mark.parametrize("directory", ["memory_resources", "context_sources"])
async def test_memory_files_resist_general_file_writes(core_app_factory, recorder, monkeypatch, tmp_path, directory):
    """普通文件工具可读记忆，但不能改写其持久文件，任务临时文件仍可写入。"""
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    monkeypatch.setattr(mas_config, "action_permission", "write")
    target = mas_config.data_dir / directory / "nested" / "source.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    original = "Remember our first conversation."
    target.write_text(original, encoding="utf-8")
    new_target = target.parent / "new.txt"
    scratch = mas_config.scratch_dir / "memory-check.txt"
    operations = [
        ("read_file", {"path": str(target)}),
        ("write_file", {"path": str(target), "content": "replacement"}),
        (
            "edit_file",
            {"path": str(target), "operation": "replace", "old_string": original, "new_string": "replacement"},
        ),
        ("delete_file", {"path": str(target)}),
        ("write_file", {"path": str(new_target), "content": "new memory"}),
        ("write_file", {"path": str(scratch), "content": "temporary notes"}),
    ]
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text="我去确认一下。<agent>检查记忆文件和临时笔记的访问边界。</agent>",
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_file_checks",
                text="",
                tool_calls=[
                    ToolCall(id=f"memory-check-{index}", name=name, arguments=json.dumps(arguments))
                    for index, (name, arguments) in enumerate(operations)
                ],
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text=_report("Memory files stayed intact; temporary notes were written.", "Checked file access."),
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="记忆还好好的，临时笔记也写好了。",
            ),
        ]
    )
    await app.start()
    await app.user_says("检查一下记忆文件的访问边界。")
    assert_clean_visible(await app.next_reply())
    assert_clean_visible(await app.next_reply(timeout=20))
    await app.wait_processed("agent_task")

    calls = [json.loads(row["payload"]) for row in app.db_query("SELECT payload FROM agent_call ORDER BY id")]
    assert len(calls) == len(operations)
    results = {call["call"]["id"]: call["result"]["text"] for call in calls}
    assert original in results["memory-check-0"]
    assert all("Access denied" in results[f"memory-check-{index}"] for index in range(1, 5))
    assert "File written successfully" in results["memory-check-5"]
    assert target.read_text(encoding="utf-8") == original
    assert not new_target.exists()
    assert scratch.read_text(encoding="utf-8") == "temporary notes"
    assert app.scripted.pending_turns == 0
    recorder.record("memory_files_preserved", directory=directory, writes_denied=4, scratch_written=True)


async def test_repeated_failure_guides_and_data_root_blocks(core_app_factory, recorder, monkeypatch, tmp_path):
    """对 data 根的散落写入连续失败两次后，熔断引导必须出现在模型可见的对话历史中。"""
    # fs_allowed_paths 覆盖 tmp_path 使 data 目录可达；拦截来自路径保护而非工具禁用
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    monkeypatch.setattr(mas_config, "action_permission", "write")
    stray = Path(mas_config.data_dir) / "stray.log"
    call = ToolCall(id="call-stray", name="write_file", arguments=json.dumps({"path": str(stray), "content": "x"}))

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text=f"交给我。<agent>把调试日志写入 {stray}</agent>",
            ),
            ScriptedTurn(when=_is_agent_request, name="agent_attempt_1", text="", tool_calls=[call]),
            ScriptedTurn(when=_is_agent_request, name="agent_attempt_2", text="", tool_calls=[call]),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text=_report("Could not write into the data root; it is protected.", "write_file denied twice"),
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="明白了，data 根目录不该乱放东西。",
            ),
        ]
    )
    await app.start()

    await app.user_says("帮我记一下这条调试日志。")
    ack = await app.next_reply()
    assert_clean_visible(ack)

    final = await app.next_reply(timeout=20)
    assert_clean_visible(final)
    await app.wait_processed("agent_task")

    # 不变量一：data 根的散落写入被真实策略拒绝，文件不存在
    assert not stray.exists()
    calls = app.db_query("SELECT status, payload FROM agent_call")
    assert len(calls) == 2
    assert all("Access denied" in row["payload"] for row in calls)

    # 不变量二：第二次相同失败后，熔断引导以独立 user 消息进入对话历史并被模型读到
    steps = [entry for entry in recorder.entries if entry["kind"] == "llm_call" and entry["name"].startswith("agent_")]
    assert [step["tool_calls"] for step in steps] == [["write_file"], ["write_file"], []]
    assert steps[2]["saw"].startswith("user: [System Guidance]")

    # 不变量三：任务以完成状态收场，剧本耗尽
    tasks = app.db_query("SELECT status FROM agent_task")
    assert [row["status"] for row in tasks] == ["completed"]
    assert app.sent == [ack, final]
    assert app.scripted.pending_turns == 0


async def test_task_execution_defaults_to_scratch_cwd(core_app_factory, monkeypatch):
    """任务内的 execute_python 不指定 cwd 时，真实进程的工作目录必须是任务专属 scratch。"""
    from muika.core.code_review import CodeReviewer, ReviewDecision

    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    monkeypatch.setattr(
        CodeReviewer,
        "assess",
        AsyncMock(
            return_value=ReviewDecision(
                decision="approve",
                effect="write",
                reason="Reads its own working directory inside the task scratchpad.",
                suggestions=[],
                impact="沙箱内自检。",
            )
        ),
    )

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text="稍等，我确认一下。<agent>打印当前工作目录并报告</agent>",
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_probe_cwd",
                text="",
                tool_calls=[
                    ToolCall(
                        id="call-cwd",
                        name="execute_python",
                        arguments=json.dumps({"code": "import os; print(os.getcwd())"}),
                    )
                ],
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text=_report("Confirmed the working directory.", "execute_python printed the scratch cwd"),
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="确认好啦。",
            ),
        ]
    )
    await app.start()

    await app.user_says("帮我确认一下你现在在哪个目录工作。")
    await app.next_reply()
    await app.next_reply(timeout=30)
    await app.wait_processed("agent_task")

    tasks = app.db_query("SELECT id, status FROM agent_task")
    assert [row["status"] for row in tasks] == ["completed"]
    task_id = tasks[0]["id"]

    # 不变量一：真实进程的 stdout 就是任务专属 scratch 目录
    calls = app.db_query("SELECT status, payload FROM agent_call")
    record = json.loads(calls[0]["payload"])
    assert record["status"] == "completed"
    process = json.loads(record["result"]["text"])
    assert process["exit_code"] == 0
    expected = str(mas_config.scratch_dir / "tasks" / task_id)
    assert Path(process["stdout"].strip()) == Path(expected)

    # 不变量二：scratch 目录真实存在于磁盘
    assert Path(expected).is_dir()
