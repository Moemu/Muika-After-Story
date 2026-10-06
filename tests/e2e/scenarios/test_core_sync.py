"""独立数据库中的真实经历同步；恢复不得生成模型回复或工具动作。"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


async def test_finished_initiative_records_cooldown_but_idle_ticks_do_not_sync(core_app_factory):
    from muika.database.db import observe_commits
    from muika.ipc.sync_store import SyncStore

    app = await core_app_factory()
    await app.start()
    assert app.muika is not None
    store = SyncStore("pc")
    store.state = app.muika.state
    await store.initialize()
    observe_commits(store.record)
    app.muika.after_activity = store.capture_snapshot
    try:
        app.scripted.add_route(when="[System]", text="<do_nothing>", name="quiet_initiative")
        app.muika.state.loneliness = 0.9
        await app.advance_time()
        await app.wait_processed("time_tick")
        entries = await store.entries()
        assert entries[-1].activity.rhythm.last_proactive_at is not None
        count = len(entries)
        await app.advance_time()
        await app.wait_processed("time_tick")
        assert len(await store.entries()) == count
    finally:
        observe_commits(None)


async def test_partition_keeps_both_diaries_and_foreground_fact(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    initial = await pc.command(action="material", kind="user", text="周末一起安排。")
    await server.command(action="apply", entries=initial["entries"])
    await pc.command(action="material", kind="user", text="改去散步。")
    foreground = await pc.command(action="dream", plan="散步", diary="PC 上商量了散步。")
    await server.command(action="material", kind="user", text="还是看电影。")
    branch = await server.command(action="dream", plan="电影", diary="服务器上商量了电影。")
    merged = await pc.command(action="apply", entries=branch["entries"], preserve_state=True)
    assert {item["content"] for item in merged["diaries"]} == {"PC 上商量了散步。", "服务器上商量了电影。"}
    assert [item["value"] for item in merged["facts"]] == ["散步"]
    remote = await server.command(action="apply", entries=foreground["entries"])
    assert [item["value"] for item in remote["facts"]] == ["散步"]
    assert {item["content"] for item in remote["diaries"]} == {"PC 上商量了散步。", "服务器上商量了电影。"}


async def test_foreground_summary_does_not_hide_other_branch(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    baseline = await pc.command(action="material", kind="user", text="共同的起点。")
    await server.command(action="apply", entries=baseline["entries"])
    await server.command(action="material", kind="user", text="摘要没有包括的远端经历。")
    await pc.command(action="material", kind="user", text="前台单独的经历。")
    summarized = await pc.command(action="summary")
    restored = await server.command(action="apply", entries=summarized["entries"])
    assert "摘要没有包括的远端经历。" in restored["turns"]


async def test_same_external_input_merges_sources_but_keeps_independent_outputs(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    source = 'ipc:["qq","same-platform-event"]'
    await pc.command(action="material", kind="user", text="同一条输入。", source=source)
    first = await pc.command(action="material", kind="muika", text="前台产生的回复。")
    await server.command(action="material", kind="user", text="同一条输入。", source=source)
    second = await server.command(action="material", kind="muika", text="远端产生的回复。")
    merged = await pc.command(action="apply", entries=second["entries"], preserve_state=True)
    assert merged["turns"].count("同一条输入。") == 1
    assert "前台产生的回复。" in merged["turns"] and "远端产生的回复。" in merged["turns"]
    repeated = await server.command(action="apply", entries=first["entries"])
    assert repeated["turns"].count("同一条输入。") == 1


async def test_foreign_queued_task_does_not_run_on_another_device(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    queued = await pc.command(action="interrupted", status="queued")
    copied = await server.command(action="apply", entries=queued["entries"])
    assert copied["tasks"][0]["status"] == "failed"
    assert copied["calls"][0]["result"]["is_error"] is True


async def test_sync_silent_state_and_independent_output(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    first = await pc.command(action="material", kind="user", text="今天可以安静待着。")
    silent = await pc.command(action="state", mood="安静而安心")
    assert len(silent["entries"]) == len(first["entries"]) + 1
    replica = await server.command(action="apply", entries=silent["entries"])
    assert replica["mood"] == "安静而安心"
    assert replica["turns"] == ["今天可以安静待着。"]
    proactive = await pc.command(action="material", kind="muika", text="我想把这首诗留给你。")
    replica = await server.command(action="apply", entries=proactive["entries"])
    repeated = await server.command(action="apply", entries=proactive["entries"])
    assert repeated["turns"] == replica["turns"] == ["今天可以安静待着。", "我想把这首诗留给你。"]


async def test_offline_histories_keep_local_ids_and_foreground_state(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    baseline = await pc.command(action="material", kind="note", text="共同的记忆基线。")
    await server.command(action="apply", entries=baseline["entries"])
    await pc.command(action="material", kind="user", text="本地散步计划。")
    left = await pc.command(action="state", mood="期待散步")
    await server.command(action="material", kind="user", text="服务器上的阅读计划。")
    right = await server.command(action="state", mood="专心阅读")
    merged_pc = await pc.command(action="apply", entries=right["entries"], preserve_state=True)
    assert merged_pc["mood"] == "期待散步"
    assert {"本地散步计划。", "服务器上的阅读计划。"} <= set(merged_pc["turns"])
    merged_server = await server.command(action="apply", entries=left["entries"])
    assert merged_server["mood"] == "期待散步"
    assert set(merged_server["turns"]) == set(merged_pc["turns"])


async def test_replica_dream_references_imported_experience(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    source = await pc.command(action="material", kind="user", text="我们准备一起散步。")
    await server.command(action="apply", entries=source["entries"])
    dream = await server.command(action="dream")
    recovered = await pc.command(action="apply", entries=dream["entries"])
    assert recovered["facts"][0]["value"] == "散步"
    assert recovered["facts"][0]["source_refs"] == ["experience:1"]


async def test_sync_survives_restart_with_existing_memory(memory_process):
    pc = await memory_process("pc")
    await pc.command(action="material", kind="user", text="重启前我们已见过面。")
    await pc.command(action="dream")
    await pc.close()
    resumed, server = await memory_process("pc"), await memory_process("server")
    saved = await resumed.command(action="state", mood="记得我们的约定")
    replica = await server.command(action="apply", entries=saved["entries"])
    assert replica["mood"] == "记得我们的约定"
    assert replica["facts"][0]["source_refs"] == ["experience:1"]
    assert replica["turns"] == ["重启前我们已见过面。"]


async def test_replica_updates_original_fact_without_duplicate_version(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    await pc.command(action="material", kind="user", text="我们想去散步。")
    original = await pc.command(action="dream")
    await server.command(action="apply", entries=original["entries"])
    await server.command(action="material", kind="user", text="我又想起了散步的约定。")
    revised = await server.command(action="dream")
    merged = await pc.command(action="apply", entries=revised["entries"])
    assert len(merged["facts"]) == 1
    assert merged["facts"][0]["id"] == original["facts"][0]["id"]
    assert merged["facts"][0]["weight"] == original["facts"][0]["weight"]
    assert len(merged["facts"][0]["source_refs"]) == 2


async def test_imported_interrupted_action_is_failure_not_replayed(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    interrupted = await pc.command(action="interrupted")
    recovered = await server.command(action="apply", entries=interrupted["entries"])
    assert recovered["tasks"][0]["status"] == "failed"
    assert recovered["calls"][0]["status"] == "completed"
    assert recovered["calls"][0]["result"]["is_error"] is True
    assert "interrupted" in recovered["calls"][0]["result"]["text"].lower()


async def test_enable_sync_on_unversioned_existing_database(memory_process, tmp_path):
    pc = await memory_process("pc")
    await pc.command(action="material", kind="user", text="升级前已有的约定。")
    await pc.command(action="dream")
    await pc.close()
    with sqlite3.connect(tmp_path / "pc" / "muika.db") as db:
        for table in ("system_state", "sync_event", "sync_reference", "sync_state", "alembic_version"):
            db.execute(f"DROP TABLE {table}")
    pc, server = await memory_process("pc"), await memory_process("server")
    saved = await pc.command(action="state", mood="仍然记得你")
    recovered = await server.command(action="apply", entries=saved["entries"])
    assert recovered["mood"] == "仍然记得你"
    assert recovered["facts"][0]["source_refs"] == ["experience:1"]


async def test_ticks_use_elapsed_time_without_creating_activity(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    small = await pc.command(action="tick", seconds=[5] * 10)
    large = await server.command(action="tick", seconds=[25] * 2)
    assert small["attention"] == pytest.approx(large["attention"])
    assert small["curiosity"] == pytest.approx(large["curiosity"])
    assert len(small["entries"]) == len(large["entries"]) == 1


async def test_recorded_curiosity_impulse_is_applied_without_inference(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    observed = await pc.command(action="curiosity", value=0.9)
    replica = await server.command(action="apply", entries=observed["entries"])
    assert replica["curiosity"] == pytest.approx(0.9, abs=0.01)


async def test_sync_wire_dates_have_offsets_and_runtime_dates_stay_local(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    observed = await pc.command(action="material", kind="user", text="不同设备上的同一段经历。")
    wire = observed["entries"][-1]["activity"]["experiences"][0]["occurred_at"]
    assert datetime.fromisoformat(wire).tzinfo is not None
    replica = await server.command(action="apply", entries=observed["entries"])
    assert datetime.fromisoformat(replica["started_at"]).tzinfo is None
    assert datetime.fromisoformat(replica["started_at"]) == datetime.fromisoformat(observed["started_at"])


async def test_rejoin_keeps_conversation_from_both_session_branches(memory_process):
    pc, server = await memory_process("pc"), await memory_process("server")
    shared = await pc.command(action="material", kind="user", text="共同的开始。")
    await server.command(action="apply", entries=shared["entries"])
    await pc.command(action="material", kind="user", text="离线时的散步。")
    await pc.command(action="state", mood="保持前台的感受")
    await server.command(action="new_session")
    other = await server.command(action="material", kind="user", text="服务器上的阅读。")
    merged = await pc.command(action="apply", entries=other["entries"], preserve_state=True)
    assert {"离线时的散步。", "服务器上的阅读。"} <= set(merged["turns"])
    assert merged["mood"] == "保持前台的感受"
