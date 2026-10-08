"""任务沙箱：data 根防护拦截散落写入、重复失败触发熔断引导、执行默认落在任务 scratch。"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.llm import ModelRequest
from muika.llm._schema import ToolCall

pytestmark = pytest.mark.e2e


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
