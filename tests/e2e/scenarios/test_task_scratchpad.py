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
