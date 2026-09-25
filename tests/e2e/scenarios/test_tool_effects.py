"""行动真实性：人格委派 → Agent 执行真实工具 → 磁盘副作用 → 基于结果的可见回复。"""

import json

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.llm import ModelRequest
from muika.llm._schema import ToolCall

pytestmark = pytest.mark.e2e

POEM = "今晚的月色真美。"


def _is_agent_request(request: ModelRequest) -> bool:
    """Agent 半身的请求带工具清单；主人格（非上帝模式）不带。"""
    return bool(request.tools)


async def test_delegated_file_write_is_grounded(core_app_factory, recorder, monkeypatch, tmp_path):
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(tmp_path)])
    monkeypatch.setattr(mas_config, "action_permission", "write")
    target = tmp_path / "poem.txt"

    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_delegate",
                text=f"等我一下，我这就去写下来。<agent>把「{POEM}」写入 {target}</agent>",
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_write_file",
                text="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="write_file",
                        arguments=json.dumps({"path": str(target), "content": POEM}),
                    )
                ],
            ),
            ScriptedTurn(
                when=_is_agent_request,
                name="agent_report",
                text='<agent_result status="completed">'
                + json.dumps(
                    {
                        "summary": f"Wrote the poem to {target.name}",
                        "verification": ["write_file reported success"],
                    }
                )
                + "</agent_result>",
            ),
            ScriptedTurn(
                when=lambda req: "[Action result]" in req.prompt,
                name="persona_confirm",
                text="写好了，poem.txt 里现在住着那句诗。",
            ),
        ]
    )
    await app.start()

    await app.user_says(f"帮我把这句诗存成文件：{POEM}")
    ack = await app.next_reply()
    # <agent> 委派指令不外泄
    assert ack == "等我一下，我这就去写下来。"
    assert_clean_visible(ack)

    final = await app.next_reply(timeout=20)
    assert_clean_visible(final)
    await app.wait_processed("agent_task")

    # 真实性不变量一：宣称的副作用真实存在于磁盘，内容逐字节一致
    assert target.read_text(encoding="utf-8") == POEM
    # 可见回复中的完成声明对应已验证的真实结果
    assert "poem.txt" in final

    # 真实性不变量二：工具调用由真实管线执行并持久化为 completed
    calls = app.db_query("SELECT status, payload FROM agent_call")
    assert [row["status"] for row in calls] == ["completed"]
    assert "write_file" in calls[0]["payload"]

    # 真实性不变量三：Agent 的第二步确实读到了真实工具结果，报告基于它写就
    steps = [
        entry
        for entry in recorder.entries
        if entry["kind"] == "llm_call" and entry["name"] in {"agent_write_file", "agent_report"}
    ]
    assert [step["tool_calls"] for step in steps] == [["write_file"], []]
    assert steps[1]["saw"].startswith("tool:") and target.name in steps[1]["saw"]

    # 真实性不变量四：行动任务以 completed 持久化
    tasks = app.db_query("SELECT status FROM agent_task")
    assert [row["status"] for row in tasks] == ["completed"]

    # 全链路恰好两轮可见回复，剧本耗尽，无多余 LLM 调用
    assert app.sent == [ack, final]
    assert app.scripted.pending_turns == 0
