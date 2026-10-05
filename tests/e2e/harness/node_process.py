"""真实 Core 启动路径，只有模型供应商被 E2E 剧本替代。"""

import asyncio
import json
import sys
from pathlib import Path

from pytest import MonkeyPatch

from muika.config import mas_config
from muika.core.memory_models import StateUpdate
from muika.database.db import init_db
from muika.ipc.bootstrap import CoreBootstrap
from muika.llm._schema import ToolCall
from muika.llm.utils.tools import dispatch_tool
from muika.plugin import load_plugins
from muika.plugin.func_call.context import tool_context

from .core_app import CoreApp
from .trace import TraceRecorder


async def main() -> None:
    directory, name, gateway, port, fallback = sys.argv[1:]
    trace = TraceRecorder(Path(directory))
    patches = MonkeyPatch()
    app = CoreApp(patches, trace)
    mas_config.data_dir = Path(directory)
    mas_config.gateway_url = gateway
    mas_config.core_node_name = name
    mas_config.local_fallback = bool(int(fallback))
    mas_config.core_priority = 100 if mas_config.local_fallback else 0
    app.scripted.add_route(
        when=lambda request: "[System]" in request.prompt, text="<do_nothing>", name="observe_environment"
    )
    app.scripted.add_route(
        when=lambda request: "[User]" in request.prompt,
        text='<state>{"mood":"期待散步","reason":"Our shared plan."}</state>我记得我们的散步约定。',
        name="conversation",
    )
    await init_db()
    load_plugins(Path("muika/builtin_plugins"))
    core = CoreBootstrap(port=int(port))
    await core.start()
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["action"] == "stop":
                break
            if command["action"] == "mood":
                await core.muika.memory.update_state(
                    StateUpdate(mood=command["value"], reason="An offline observation.")
                )
            tool_result = None
            if command["action"] == "remind":
                await core.muika.executor.scheduler.schedule(command["text"], trigger_in_seconds=command["delay"])
            if command["action"] == "handoff":
                with tool_context(core.muika.state, core.muika.executor):
                    tool_result = await dispatch_tool(
                        ToolCall(
                            id="handoff", name="request_handoff", arguments=json.dumps({"target": command["target"]})
                        )
                    )
            assert core.node is not None
            print(
                json.dumps(
                    {
                        "active": core.node.active,
                        "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
                        "connected": core.node.connected,
                        "ready": core.node.name in core.node.nodes,
                        "model_calls": len(app.scripted.calls),
                        "conversations": sum(call["name"] == "conversation" for call in app.scripted.calls),
                        "mood": core.muika.memory.persistent.mood,
                        "turns": [turn.content for turn in core.muika.memory.recent_turns],
                        "reminders": sum("A scheduled reminder" in call["prompt"] for call in app.scripted.calls),
                    }
                ),
                flush=True,
            )
    finally:
        await core.stop()
        trace.write()
        patches.undo()


if __name__ == "__main__":
    asyncio.run(main())
