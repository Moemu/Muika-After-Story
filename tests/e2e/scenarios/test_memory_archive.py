"""<memory> 归档链路：决定记住的内容落入素材表，不外泄、不混进对话回合。"""

import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from harness import ScriptedTurn, assert_clean_visible

from muika.config import mas_config
from muika.core.agent.task_store import CallRecord, TaskRecord, TaskStore
from muika.core.memory_models import DreamResult, Intention, StateUpdate
from muika.ipc.sync_models import Activity
from muika.ipc.sync_store import SyncStore
from muika.llm._schema import ModelMessage, ToolCall, ToolResult

pytestmark = pytest.mark.e2e

NOTE = "Master 在雨天喜欢临窗读诗。"


async def test_cancelled_wish_can_be_considered_again_after_restart(core_app_factory, recorder):
    app = await core_app_factory()
    await app.start()
    await app.muika.memory.update_state(
        StateUpdate(reason="I am curious.", intentions=[Intention(id="read_poem", description="想读一首诗。")])
    )
    old = TaskRecord(instruction="读诗", original_request="自己的兴趣", intention_id="read_poem", status="cancelled")
    await TaskStore().save(old)
    await app.stop()
    returned = await core_app_factory()
    await returned.start()
    await returned.muika.agent_tasks.initialize()
    assert returned.muika.memory.persistent.intentions[0].task_id is None
    new = await returned.muika.agent_tasks.submit("再读一首诗", "自己的兴趣", intention_id="read_poem")
    assert new.id != old.id
    recorder.record("wish_reconsidered", cancelled_task_reused=False, new_task_id=new.id)


async def test_migration_keeps_personal_sources_when_node_ids_collide(core_app_factory, recorder):
    app = await core_app_factory()
    await app.start()
    await app.stop()
    root = Path(__file__).resolve().parents[3]
    result = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-c",
            "from alembic import command; from alembic.config import Config; import sys; "
            "cfg=Config(sys.argv[1]); cfg.set_main_option('script_location', sys.argv[2]); "
            "cfg.set_main_option('sqlalchemy.url', sys.argv[3]); command.downgrade(cfg, '7ae1ecb1f246')",
            str(root / "alembic.ini"),
            str(root / "muika/migrations"),
            "sqlite+aiosqlite:///" + str(mas_config.data_dir / "muika.db"),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sources = {"experience:1": "other:experience:99", "experience:2": "pc:experience:2"}
    experiences = [
        {
            "id": 1,
            "session_id": "old",
            "kind": "user",
            "content": "我回来了。",
            "occurred_at": "2026-10-06T20:00:00",
            "source": None,
        },
        {
            "id": 2,
            "session_id": "old",
            "kind": "agent",
            "content": "private-tool-payload",
            "occurred_at": "2026-10-06T20:01:00",
            "source": "task_call:private-call",
        },
    ]
    diary = {
        "id": 1,
        "source": "dream:2026-10-06",
        "day": "2026-10-06",
        "content": "我很想他。",
        "source_refs": ["experience:2"],
        "covered_through": 2,
        "created_at": "2026-10-07T00:00:00",
    }
    activity = {
        "id": "old",
        "origin": "pc",
        "references": sources,
        "experiences": experiences,
        "diaries": [diary],
        "tasks": [],
        "calls": [],
    }
    with sqlite3.connect(mas_config.data_dir / "muika.db") as db:
        for item in experiences:
            db.execute(
                "INSERT INTO experience VALUES (:id, :session_id, :kind, :content, :occurred_at, '[]', :source)", item
            )
        db.execute(
            "INSERT INTO diary VALUES (:id, :source, :day, :content, :source_refs, :covered_through, :created_at)",
            {**diary, "source_refs": json.dumps(diary["source_refs"])},
        )
        db.executemany(
            "INSERT INTO sync_reference VALUES (?, ?)",
            [("pc:experience:1", 1), ("pc:experience:2", 2), ("other:experience:99", 1)],
        )
        db.execute("INSERT INTO sync_event(id,origin,payload) VALUES ('old','pc',?)", (json.dumps(activity),))
    restarted = await core_app_factory()
    await restarted.start()
    assert restarted.db_query("SELECT covered_through FROM diary") == [{"covered_through": 1}]
    assert not restarted.db_query("SELECT id FROM experience WHERE source LIKE 'task_call:%'")
    saved = Activity.model_validate_json(restarted.db_query("SELECT payload FROM sync_event")[0]["payload"])
    assert saved.references["experience:1"] == "other:experience:99"
    assert saved.references[f"experience:{saved.diaries[0].covered_through}"] == "pc:experience:1"
    store = SyncStore("pc")
    await store.initialize()
    await store.capture_snapshot()
    assert await store.entries()
    recorder.record("migration_source_collision", original_reference_preserved=True, diary_watermark_valid=True)


async def test_dream_uses_personal_experiences_without_execution_state(core_app_factory):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    await app.muika.memory.add_context("muika", "二十三天，我很想你。", timestamp=datetime(2026, 10, 6, 20, 1))
    await app.muika.memory.update_state(StateUpdate(mood="private-runtime-marker", reason="private-stack-trace"))
    await app.muika.memory.add_material("state", '{"mood":"private-state-marker"}', timestamp=datetime(2026, 10, 6, 23))
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream",
        text=DreamResult(diary="沐沐回来了。我想念他，也想知道他是否还记得我。").model_dump_json(),
        name="personal_dream",
    )
    assert await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    call = next(call for call in app.scripted.calls if call["name"] == "personal_dream")
    assert "二十三天" in call["prompt"]
    assert "Monika" in call["system"]
    assert "private-runtime-marker" not in str(call)
    assert "private-stack-trace" not in str(call)
    assert "private-state-marker" not in str(call)
    assert not await app.muika.memory.pending_days(datetime.now(), include_today=True)
    app.recorder.record("personal_dream", runtime_state_injected=False, diary_saved=True)


@pytest.mark.parametrize("malformed", [False, True], ids=["numeric_references", "invalid_json"])
async def test_dream_repairs_original_result_once_before_saving(core_app_factory, malformed):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    ref = await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    result = {
        "diary": "沐沐回来了。我很想他，也想把这些没说出口的话留住。",
        "facts": [{"category": "relation", "key": "master.return", "value": "沐沐回来了。", "source_refs": [ref]}],
        "state_update": {
            "reason": "His return matters to me.",
            "intentions": [{"id": "share_poem", "description": "想和沐沐读诗。", "source_refs": [ref]}],
        },
        "dissonance_delta": -0.02,
        "tension_reason": "He came back.",
        "tension_source_refs": [ref],
        "relief": "positive_feedback",
    }
    invalid = '{"diary":' if malformed else json.dumps(result, ensure_ascii=False)
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream" and "Validation errors:" not in req.prompt,
        text=invalid,
        name="invalid_dream",
    )
    result["facts"][0]["source_refs"] = [f"experience:{ref}"]
    result["state_update"]["intentions"][0]["source_refs"] = [f"experience:{ref}"]
    result["tension_source_refs"] = [f"experience:{ref}"]
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream" and "Validation errors:" in req.prompt,
        text="Here is the repaired result:\n```json\n" + json.dumps(result, ensure_ascii=False) + "\n```",
        name="repaired_dream",
    )
    session_id = app.muika.memory.snapshot.session.session_id
    assert await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    calls = [call for call in app.scripted.calls if call["name"] in {"invalid_dream", "repaired_dream"}]
    assert len(calls) == 2
    assert invalid in calls[1]["prompt"]
    assert ("json_invalid" if malformed else "facts.0.source_refs.0") in calls[1]["prompt"]
    assert app.db_query("SELECT content FROM diary") == [{"content": result["diary"]}]
    assert json.loads(app.db_query("SELECT source_refs FROM fact")[0]["source_refs"]) == [f"experience:{ref}"]
    assert app.muika.memory.persistent.intentions[0].source_refs == [f"experience:{ref}"]
    assert app.muika.memory.snapshot.session.session_id == session_id
    assert not await app.muika.memory.pending_days(datetime.now(), include_today=True)
    app.recorder.record("dream_repaired", attempts=2, diary_saved=True, session_preserved=True)


async def test_dream_repeated_format_failure_leaves_memory_unchanged(core_app_factory):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    before = app.muika.memory.snapshot.model_dump()
    app.scripted.add_route(when=lambda req: req.purpose == "memory_dream", text='{"diary":', name="invalid_dream")
    with pytest.raises(ValueError):
        await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    assert len([call for call in app.scripted.calls if call["name"] == "invalid_dream"]) == 2
    assert not app.db_query("SELECT id FROM diary")
    assert not app.db_query("SELECT id FROM fact")
    assert app.muika.memory.snapshot.model_dump() == before
    assert day in await app.muika.memory.pending_days(datetime.now(), include_today=True)
    app.recorder.record("dream_repair_exhausted", attempts=2, diary_saved=False, material_pending=True)


async def test_dream_oversized_repair_keeps_material_pending(core_app_factory):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    ref = await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    app.scripted.config.context_window = 32768
    app.scripted.config.max_tokens = 2048
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream",
        text=json.dumps(
            {
                "diary": "I missed him. " * 10000,
                "tension_source_refs": [ref],
            }
        ),
        name="oversized_invalid_dream",
    )
    before = app.muika.memory.snapshot.model_dump()
    with pytest.raises(ValueError, match="repair exceeds the context budget"):
        await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    assert len([call for call in app.scripted.calls if call["name"] == "oversized_invalid_dream"]) == 1
    assert not app.db_query("SELECT id FROM diary")
    assert app.muika.memory.snapshot.model_dump() == before
    assert day in await app.muika.memory.pending_days(datetime.now(), include_today=True)
    app.recorder.record("dream_repair_over_budget", attempts=1, diary_saved=False, material_pending=True)


@pytest.mark.parametrize("field", ["facts", "intentions"])
async def test_dream_rejects_unknown_source_without_saving(core_app_factory, field):
    app = await core_app_factory()
    await app.start()
    day = date(2026, 10, 6)
    await app.muika.memory.add_context("user", "我回来了。", timestamp=datetime(2026, 10, 6, 20))
    result = {"diary": "我很想他。"}
    if field == "facts":
        result["facts"] = [
            {"category": "relation", "key": "master.return", "value": "回来了。", "source_refs": ["experience:999"]}
        ]
    else:
        result["state_update"] = {
            "reason": "I want to share a poem.",
            "intentions": [{"id": "poem", "description": "想读诗。", "source_refs": ["experience:999"]}],
        }
    before = app.muika.memory.snapshot.model_dump()
    app.scripted.add_route(
        when=lambda req: req.purpose == "memory_dream", text=json.dumps(result), name="unknown_source"
    )
    with pytest.raises(ValueError, match="not supplied"):
        await app.muika.agent.memory_reasoner.dream(day, app.muika.memory)
    assert not app.db_query("SELECT id FROM diary")
    assert not app.db_query("SELECT id FROM fact")
    assert app.muika.memory.snapshot.model_dump() == before
    app.recorder.record("dream_unknown_source", field=field, diary_saved=False)


async def test_execution_checkpoint_stays_outside_database(core_app_factory):
    app = await core_app_factory()
    await app.start()
    store = TaskStore()
    call = ToolCall(id="private-call", name="read_file", arguments='{"path":"private-source.py"}')
    task = TaskRecord(instruction="看看窗外的天气", original_request="你想做什么？")
    task.messages = [ModelMessage(role="assistant", tool_calls=[call])]
    record = CallRecord(task_id=task.id, call=call, result=ToolResult(text="private-tool-output"))
    await store.save(task, record)
    assert (await TaskStore().load())[0].messages == task.messages
    assert (await TaskStore().calls(task.id))[0] == record
    tables = app.db_query("SELECT name FROM sqlite_master WHERE type='table'")
    assert not {"agent_task", "agent_call"} & {row["name"] for row in tables}
    assert not app.db_query("SELECT id FROM experience WHERE source LIKE 'task_call:%'")
    app.recorder.record("file_checkpoint", task_id=task.id, recovered_calls=1, database_execution_tables=0)


async def test_memory_tag_is_archived_not_leaked(core_app_factory):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                when=lambda req: "[User]" in req.prompt,
                name="persona_note",
                text=f"<memory>{NOTE}</memory>嗯……这件事我会好好收着的。",
            )
        ]
    )
    await app.start()

    await app.user_says("告诉你一个小秘密：雨天的时候我最喜欢一个人待着。")
    reply = await app.next_reply()
    await app.wait_processed("user_message")

    # 可见回复干净：<memory> 标签与内容都不外露
    assert reply == "嗯……这件事我会好好收着的。"
    assert_clean_visible(reply)
    assert NOTE not in reply

    # 归档不变量：内容以 note 素材真实落库
    notes = app.db_query("SELECT content FROM experience WHERE kind = 'note'")
    assert any(NOTE in row["content"] for row in notes)

    # note 不是对话回合：不进入 recent_turns，也不会随历史送回模型
    assert all(NOTE not in turn.content for turn in app.muika.memory.recent_turns)

    assert app.scripted.pending_turns == 0
