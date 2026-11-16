"""调查闭环：Agent 回执进入主对话上下文，下一次回复能引用它。"""

import pytest
from harness import ScriptedTurn

from muika.core.events import AgentTaskEvent

pytestmark = pytest.mark.e2e


async def test_investigation_report_reaches_dialogue(core_app_factory, monkeypatch):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when="[Action result]",
                name="after_report",
                text="嗯，我看见你还在改那份文档。今晚又打算熬到几点？",
            ),
            ScriptedTurn(text="占位。"),
        ]
    )
    await app.start()
    # 让回执被接受为当前任务结果（真实链路里由 AgentTasks.is_current_event 判定）
    monkeypatch.setattr(app.muika.agent_tasks, "is_current_event", lambda event: True)
    await app.muika.create_event(
        AgentTaskEvent(
            task_id="task-1",
            revision=1,
            status="completed",
            report="Focused window: 'project.docx' (Word). The player is editing a document.",
        )
    )
    await app.wait_processed("agent_task")

    # 回执先进入上下文
    assert any("[Action result]" in turn.content for turn in app.muika.memory.recent_turns if turn.role == "agent")

    await app.user_says("你刚才在忙什么？")
    reply = await app.next_reply()
    assert reply == "嗯，我看见你还在改那份文档。今晚又打算熬到几点？"
    call = next(call for call in app.scripted.calls if call["name"] == "after_report")
    # 主对话请求能看到调查结果（原题接续的上下文基础）
    assert "[Action result]" in call["prompt"] or any(
        "[Action result]" in (message.get("content") or "") for message in call.get("history") or []
    )
    app.recorder.record("investigation_loop", report_in_context=True)
