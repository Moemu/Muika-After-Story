"""验证重启后的任务现场保留，以及搜索失败进入恢复管线的真实状态。"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.core.actions.tools import _search
from muika.core.agent import task_store
from muika.llm._schema import ToolCall

pytestmark = pytest.mark.e2e


def _report(status: str) -> str:
    return (
        f'<agent_result status="{status}">'
        + json.dumps({"summary": "Draft saved." if status == "completed" else "Waiting for clarification."})
        + "</agent_result>"
    )


@pytest.mark.parametrize(
    ("status", "checkpoint_age_days", "preserved"),
    [("blocked", 4, True), ("completed", 0, True), ("completed", 4, False)],
)
async def test_restart_retains_resumable_and_recent_task_work(
    core_app_factory, recorder, monkeypatch, approved_review, tmp_path, status, checkpoint_age_days, preserved
):
    """旧目录不能导致受阻任务或刚完成的长任务丢失文件，过期已完成任务仍应清理。"""
    monkeypatch.setattr(mas_config, "action_permission", "write")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    monkeypatch.setattr(mas_config, "scratch_retention_days", 3)
    checkpoint_time = datetime.now(timezone.utc) - timedelta(days=checkpoint_age_days)
    monkeypatch.setattr(task_store, "_now", lambda: checkpoint_time.isoformat())
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                name="delegate",
                when=lambda req: not req.tools,
                text="我先保存草稿。<agent>保存草稿，等待后续安排。</agent>",
            ),
            ScriptedTurn(
                name="write_draft",
                when=lambda req: bool(req.tools),
                text="",
                tool_calls=[
                    ToolCall(
                        id="write-draft",
                        name="execute_python",
                        arguments=json.dumps(
                            {
                                "code": (
                                    "from pathlib import Path; "
                                    "Path('draft.txt').write_text('unfinished draft'); print('saved')"
                                ),
                                "yield_time": 5,
                            }
                        ),
                    ),
                ],
            ),
            ScriptedTurn(name="task_report", when=lambda req: bool(req.tools), text=_report(status)),
            ScriptedTurn(name="report_to_player", when=lambda req: not req.tools, text="草稿保存好了。"),
        ]
    )
    await app.start()
    await app.user_says("先帮我准备草稿。")
    assert_clean_visible(await app.next_reply())
    assert_clean_visible(await app.next_reply(timeout=30))
    await app.wait_processed("agent_task")
    task = app.db_query("SELECT id, status FROM agent_task")[0]
    assert task["status"] == status
    directory = mas_config.scratch_dir / "tasks" / task["id"]
    draft = directory / "draft.txt"
    assert draft.read_text() == "unfinished draft"
    await app.stop()
    old_directory_time = (datetime.now(timezone.utc) - timedelta(days=4)).timestamp()
    os.utime(directory, (old_directory_time, old_directory_time))

    restarted = await core_app_factory(
        turns=(
            [
                ScriptedTurn(
                    name="continue_task",
                    when=lambda req: not req.tools,
                    text=(f'我接着检查。<agent task_id="{task["id"]}" action="continue">读取已有草稿并完成。</agent>'),
                ),
                ScriptedTurn(
                    name="read_draft",
                    when=lambda req: bool(req.tools),
                    text="",
                    tool_calls=[
                        ToolCall(
                            id="read-draft",
                            name="execute_python",
                            arguments=json.dumps(
                                {
                                    "code": "from pathlib import Path; print(Path('draft.txt').read_text())",
                                    "yield_time": 5,
                                }
                            ),
                        ),
                    ],
                ),
                ScriptedTurn(name="complete_task", when=lambda req: bool(req.tools), text=_report("completed")),
                ScriptedTurn(name="continued_report", when=lambda req: not req.tools, text="已经接着检查完了。"),
            ]
            if status == "blocked"
            else ()
        )
    )
    await restarted.start()
    assert restarted.muika is not None
    await restarted.muika.agent_tasks.initialize()
    recorder.record(
        "scratch_after_restart", task_status=status, preserved=draft.exists(), checkpoint_age_days=checkpoint_age_days
    )
    assert draft.exists() is preserved
    if status == "blocked":
        await restarted.user_says("继续刚才的草稿。")
        assert_clean_visible(await restarted.next_reply())
        assert_clean_visible(await restarted.next_reply(timeout=30))
        await restarted.wait_processed("agent_task")
        records = [json.loads(row["payload"]) for row in restarted.db_query("SELECT payload FROM agent_call")]
        read_result = next(row["result"] for row in records if row["call"]["id"] == "read-draft")
        process = json.loads(read_result["text"])
        assert process["exit_code"] == 0
        assert process["stdout"].strip() == "unfinished draft"
        assert restarted.db_query("SELECT id, status FROM agent_task") == [{"id": task["id"], "status": "completed"}]
        assert restarted.scripted.pending_turns == 0


@pytest.mark.parametrize("outcome", ["timeout", "unconfigured", "invalid_range", "empty_query", "unknown", "empty"])
async def test_search_failure_status_and_retry_guidance(core_app_factory, recorder, monkeypatch, outcome):
    """搜索失败必须保留错误标记并触发重复失败引导，正常空结果不能触发引导。"""

    async def search(query, time_range):
        if outcome == "timeout":
            raise TimeoutError("simulated timeout")
        return []

    monkeypatch.setattr(mas_config, "web_search_provider", "tavily")
    monkeypatch.setattr(mas_config, "web_search_api_key", "test-key")
    monkeypatch.setitem(_search.SEARCH_BACKENDS, "tavily", search)
    arguments = {"query": "release news"}
    if outcome == "unconfigured":
        monkeypatch.setattr(mas_config, "web_search_api_key", "")
    elif outcome == "unknown":
        monkeypatch.setattr(mas_config, "web_search_provider", "unknown")
    elif outcome == "invalid_range":
        arguments["time_range"] = "invalid"
    elif outcome == "empty_query":
        arguments["query"] = " "
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                name="delegate_search", when=lambda req: not req.tools, text="我去查一下。<agent>搜索最新消息。</agent>"
            ),
            *[
                ScriptedTurn(
                    name=f"search_{attempt}",
                    when=lambda req: bool(req.tools),
                    text="",
                    tool_calls=[
                        ToolCall(id=f"search-{attempt}", name="web_search", arguments=json.dumps(arguments)),
                    ],
                )
                for attempt in range(2)
            ],
            ScriptedTurn(
                name="search_report",
                when=lambda req: bool(req.tools),
                text=_report("completed" if outcome == "empty" else "blocked"),
            ),
            ScriptedTurn(name="search_to_player", when=lambda req: not req.tools, text="这次没有查到可用的信息。"),
        ]
    )
    await app.start()
    await app.user_says("帮我查一下最新消息。")
    assert_clean_visible(await app.next_reply())
    assert_clean_visible(await app.next_reply(timeout=20))
    await app.wait_processed("agent_task")
    records = [json.loads(row["payload"]) for row in app.db_query("SELECT payload FROM agent_call")]
    failed = outcome != "empty"
    assert len(records) == 2
    assert all(row["result"]["is_error"] is failed for row in records)
    report = next(
        entry for entry in recorder.entries if entry["kind"] == "llm_call" and entry["name"] == "search_report"
    )
    assert ("[System Guidance]" in report["saw"]) is failed
    recorder.record("search_result_status", outcome=outcome, is_error=failed, retry_guidance=failed)
    assert app.scripted.pending_turns == 0
