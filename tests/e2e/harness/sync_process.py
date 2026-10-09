"""用独立进程运行本地记忆与同步日志，命令仅由 E2E 场景提供。"""

import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from muika.config import mas_config
from muika.core.agent.task_store import CallRecord, TaskRecord, TaskStore
from muika.core.events import TimeTickEvent
from muika.core.memory import MemoryManager
from muika.core.memory_models import DreamResult, StateUpdate
from muika.core.state import MuikaState
from muika.database.db import close_db, get_session, init_db, observe_commits
from muika.database.orm_models import ExperienceORM, MemoryRuntimeORM
from muika.ipc.sync_models import SyncEntry
from muika.ipc.sync_store import SyncStore
from muika.llm._schema import ToolCall


async def main() -> None:
    directory = Path(sys.argv[1])
    directory.mkdir(parents=True, exist_ok=True)
    mas_config.data_dir = directory
    await init_db(directory / "muika.db")
    memory = MemoryManager()
    await memory.load()
    store = SyncStore(sys.argv[2])
    state = MuikaState(memory=memory)
    store.state = state
    await store.initialize()
    observe_commits(store.record)
    try:
        while line := await asyncio.to_thread(sys.stdin.readline):
            command = json.loads(line)
            if command["action"] == "material":
                await memory.add_material(command["kind"], command["text"], source=command.get("source"))
            elif command["action"] == "state":
                await memory.update_state(StateUpdate(mood=command["mood"], reason="A real observation."))
            elif command["action"] == "new_session":
                await memory.new_session()
            elif command["action"] == "summary":
                snapshot = memory.snapshot.model_copy(deep=True)
                snapshot.working_summary = "Saved foreground conversation summary."
                snapshot.summary_through = memory.recent_turns[-1].id
                snapshot.latest_dialogue_summary = snapshot.working_summary
                snapshot.dialogue_summary_through = snapshot.summary_through
                snapshot.dialogue_summary_at = datetime.now()
                async with get_session() as db:
                    await db.merge(MemoryRuntimeORM(id=1, payload=snapshot.model_dump_json()))
                await memory.load(record_activity=False)
            elif command["action"] == "tick":
                for seconds in command["seconds"]:
                    state.tick_state(TimeTickEvent(), seconds)
            elif command["action"] == "curiosity":
                state.curiosity = command["value"]
                await memory.add_material("note", "I found something interesting.")
            elif command["action"] == "dream":
                day = datetime.now().date()
                material = await memory.day_material(day)
                evidence = next(item for item in reversed(material) if item.kind == "user")
                result = DreamResult.model_validate(
                    {
                        "diary": command.get("diary", "我记住了我们商量的计划。"),
                        "facts": [
                            {
                                "category": "relation",
                                "key": "shared.plan",
                                "value": command.get("plan", "散步"),
                                "source_refs": [f"experience:{evidence.id}"],
                            }
                        ],
                    }
                )
                await memory.save_dream(
                    day, result, max(item.id for item in material), {f"experience:{item.id}" for item in material}
                )
            elif command["action"] == "apply":
                for entry in command["entries"]:
                    await store.apply(
                        SyncEntry.model_validate(entry), preserve_state=command.get("preserve_state", False)
                    )
                await memory.load(record_activity=False)
            elif command["action"] == "interrupted":
                task = TaskRecord(
                    status=command.get("status", "running"), instruction="Write a file", original_request="Write a file"
                )
                call = CallRecord(task_id=task.id, call=ToolCall(id="call-1", name="write_file", arguments="{}"))
                await TaskStore().save(task, call)
            elif command["action"] == "stop":
                break
            async with get_session(record_activity=False) as db:
                dialogue = list(
                    await db.scalars(
                        select(ExperienceORM.content)
                        .where(ExperienceORM.kind.in_(["user", "muika", "agent"]))
                        .order_by(ExperienceORM.id)
                    )
                )
            print(
                json.dumps(
                    {
                        "entries": [entry.model_dump(mode="json") for entry in await store.entries()],
                        "mood": memory.persistent.mood,
                        "attention": state.attention,
                        "curiosity": state.curiosity,
                        "started_at": memory.session.started_at.isoformat(),
                        "facts": [fact.model_dump(mode="json") for fact in memory.facts.values()],
                        "diaries": [
                            diary.model_dump(mode="json")
                            for diary in await memory.recent_diaries(
                                datetime.now().date() + timedelta(days=1), limit=50
                            )
                        ],
                        "turns": [turn.content for turn in memory.recent_turns],
                        "summary": memory.snapshot.latest_dialogue_summary,
                        "experiences": dialogue,
                        "tasks": [task.model_dump(mode="json") for task in await TaskStore().load()],
                        "calls": [
                            call.model_dump(mode="json")
                            for task in await TaskStore().load()
                            for call in await TaskStore().calls(task.id)
                        ],
                    }
                ),
                flush=True,
            )
    finally:
        observe_commits(None)
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
