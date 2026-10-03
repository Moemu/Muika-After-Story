"""运行真实节点进程，模型使用可核对的剧本，不替换传输或状态业务。"""

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn
from harness.trace import TraceRecorder

from muika.config import mas_config
from muika.database.db import close_db, init_db
from muika.node.__main__ import serve
from muika.node.config import NodeProfile, ServerProfile
from muika.node.core_node import CoreNode


async def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("role", choices=["state", "core"])
    cli.add_argument("profile", type=Path)
    cli.add_argument("--turns", type=Path)
    args = cli.parse_args()
    if args.role == "state":
        await serve(ServerProfile.read(args.profile))
        return
    profile = NodeProfile.read(args.profile)
    recorder = TraceRecorder(profile.directory)
    with pytest.MonkeyPatch.context() as monkeypatch:
        turns = [ScriptedTurn(text=text) for text in json.loads(args.turns.read_text(encoding="utf-8"))]
        app = CoreApp(monkeypatch, recorder, turns=turns)
        app.scripted.add_route(
            when=lambda request: request.prompt.partition("] ")[2].startswith("[Runtime observation]"),
            text="<do_nothing>",
            name="quiet_device_observation",
        )
        mas_config.data_dir = profile.directory
        await init_db(profile.directory / "device-audit.db")
        node = CoreNode(
            profile.address, profile.token, profile.id, profile.directory, lease_seconds=profile.lease_seconds
        )
        job = asyncio.create_task(node.run())
        try:
            await node.ready.wait()
            (profile.directory / "ready.json").write_text(json.dumps({"id": node.node_id}), encoding="utf-8")
            await job
        finally:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            recorder.write()
            await close_db()


if __name__ == "__main__":
    asyncio.run(main())
