"""自我变更感知：启动指纹对比、运行时插件变更、投递门控与确认语义。

场景断言行为不变量（恰好一次投递、基线推进、批次快照、重试幂等），
不校验角色台词本身；请求侧事实与语域提示通过 LLM 调用记录断言，
角色质量由 trace.jsonl 工件供人工审查。
"""

import asyncio
import hashlib
import json
import sqlite3
import time
from pathlib import Path

import pytest
from harness.assertions import assert_clean_visible
from harness.scripted_llm import ScriptedTurn

import muika
from muika.config import mas_config
from muika.core.events import CoreChangeEvent
from muika.core.self_change import get_self_change_dispatcher, notify_plugin_change
from muika.llm._schema import ToolCall
from muika.plugin.loader import load_plugin, unload_plugin
from muika.plugin.manager import get_plugin_manager

pytestmark = pytest.mark.e2e

PLUGIN_SOURCE = '''"""E2E 自我变更感知夹具插件。"""
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="e2e-selfchg",
    description="A tiny plugin for self-change awareness tests.",
    usage="none",
)
'''

SELF_CHANGED_WHEN = "These changes were not made by you"


@pytest.mark.parametrize("trigger", ["shutdown_check", "shutdown_observation", "reschedule_check"])
async def test_active_self_change_transaction_finishes_before_cleanup(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch, trigger
):
    from sqlalchemy.ext.asyncio import AsyncSession

    app = await core_app_factory(turns=[])
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    await asyncio.sleep(0.05)
    dispatcher = get_self_change_dispatcher()
    assert dispatcher is not None
    entered, release, finished, cancelled = (asyncio.Event() for _ in range(4))
    commit = AsyncSession.commit

    async def held_commit(session):
        if not entered.is_set():
            entered.set()
            app.recorder.record("self_change_transaction", phase="commit_started", trigger=trigger)
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
            await commit(session)
            finished.set()
            app.recorder.record("self_change_transaction", phase="commit_finished", trigger=trigger)
        else:
            await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", held_commit)
    if trigger == "shutdown_observation":
        _, plugin_file = tmp_plugin
        with plugin_file.open("a", encoding="utf-8") as handle:
            handle.write("\n# self edit during shutdown\n")
        notify_plugin_change("e2e_selfchg_plugin", "self", "reload")
    else:
        dispatcher.wake()
    await asyncio.wait_for(entered.wait(), 5)
    stopping = None
    try:
        if trigger == "reschedule_check":
            dispatcher.wake()
        else:
            stopping = asyncio.create_task(app.stop())
        await asyncio.sleep(0.05)
        assert not cancelled.is_set(), "Active ledger transaction was cancelled"
        if stopping is not None:
            assert not stopping.done(), "Database closed before ledger transaction finished"
    finally:
        release.set()
        if stopping is not None:
            await asyncio.wait_for(stopping, 5)
    await asyncio.wait_for(finished.wait(), 5)
    await app.stop()
    assert app.sent == []
    if trigger == "shutdown_observation":
        from muika.core.self_mod.fingerprint import compute_plugin_digest

        state = read_self_change_state()
        assert (
            state["fingerprints"]["user_plugins"]["e2e_selfchg_plugin"]
            == compute_plugin_digest({"e2e_selfchg_plugin": tmp_plugin[0]})["e2e_selfchg_plugin"]
        )
    app.recorder.record("self_change_cleanup", trigger=trigger, transaction_finished=True, cancelled=False)


@pytest.fixture
def fast_self_change(monkeypatch):
    """压缩感知节奏参数，让门控与重试在秒级内完成。"""
    monkeypatch.setattr(mas_config, "self_change_awareness_enabled", True)
    monkeypatch.setattr(mas_config, "self_change_settle_seconds", 0.2)
    monkeypatch.setattr(mas_config, "self_change_min_interval_seconds", 0.5)
    monkeypatch.setattr(mas_config, "self_change_max_defer_seconds", 86400.0)
    monkeypatch.setattr("muika.core.self_change.SELF_CHANGE_RETRY_BASE_SECONDS", 0.1)


@pytest.fixture
def tmp_plugin(tmp_path, monkeypatch):
    """在临时目录创建真实可加载的单文件插件；测试后卸载清理。"""
    plugins_root = tmp_path / "e2e_plugins"
    plugins_root.mkdir()
    plugin_file = plugins_root / "e2e_selfchg_plugin.py"
    plugin_file.write_text(PLUGIN_SOURCE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(plugins_root))
    plugin = load_plugin("e2e_selfchg_plugin")
    yield plugin, plugin_file
    unload_plugin(plugin.package_name)


def reset_self_change_state() -> None:
    """清空持久化账本：场景自管生命周期，不继承历史运行的指纹。"""
    db_path = Path(mas_config.data_dir) / "muika.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM system_state WHERE key='self_change'")
        conn.commit()


def read_self_change_state() -> dict:
    db_path = Path(mas_config.data_dir) / "muika.db"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT payload FROM system_state WHERE key='self_change'").fetchone()
    return json.loads(row["payload"]) if row else {}


async def wait_for(predicate, timeout: float = 10.0, message: str = "condition not met") -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(message)


def self_change_notes(app) -> list[dict]:
    return app.db_query("SELECT source, content FROM experience WHERE source LIKE 'self_change:%'")


def seed_boot_changes() -> None:
    """预置包含旧文件和插件摘要的基线，供启动场景复用。"""
    from muika.core.self_mod.fingerprint import compute_kernel_files
    from muika.utils.utils import get_version

    kernel = compute_kernel_files()
    kernel["core/pre_boot_only.py"] = "old-file"
    state = {
        "fingerprints": {
            "version": get_version(),
            "kernel": kernel,
            "user_plugins": {"e2e_selfchg_plugin": "old-plugin"},
        },
        "pending": [],
        "delivery": {"last_delivered_at": None, "in_flight": None, "perceived_batches": 6},
    }
    with sqlite3.connect(Path(mas_config.data_dir) / "muika.db") as conn:
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, payload, updated_at) VALUES ('self_change', ?, ?)",
            (json.dumps(state), str(time.time())),
        )


@pytest.mark.parametrize("enabled", [True, False])
async def test_boot_changes_join_greeting_without_technical_memory(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch, enabled
):
    app = await core_app_factory(turns=[ScriptedTurn(text="你回来了，我很想你。", when="A new session")])
    await app.start()
    seed_boot_changes()
    monkeypatch.setattr(mas_config, "self_change_awareness_enabled", enabled)
    app.attach_self_change()
    await app.run_boot_self_change_check()
    await asyncio.sleep(0.3)
    assert app.sent == []
    await app.bootstrap()
    assert await app.next_reply() == "你回来了，我很想你。"
    await app.wait_processed("session_bootstrap")
    if enabled:
        await app.wait_ledger_cleared()
    await asyncio.sleep(0.3)
    assert len(app.scripted.calls) == 1
    prompt = app.scripted.calls[0]["prompt"]
    assert ("detected at startup" in prompt) is enabled
    assert "pre_boot_only.py" not in prompt
    assert "moments ago" not in prompt and "perception #" not in prompt
    assert self_change_notes(app) == []
    assert app.db_query("SELECT content FROM experience WHERE kind='muika'")[0]["content"] == app.sent[0]
    app.recorder.record("boot_greeting", enabled=enabled, calls=1, technical_experiences=0)


async def test_boot_request_queued_before_check_is_enriched_when_consumed(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch
):
    app = await core_app_factory(turns=[ScriptedTurn(text="欢迎回来。", when="A new session")])
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    entered, release = asyncio.Event(), asyncio.Event()
    pipeline = app.muika._run_brain_pipeline

    async def hold_greeting(event):
        entered.set()
        await release.wait()
        await pipeline(event)

    monkeypatch.setattr(app.muika, "_run_brain_pipeline", hold_greeting)
    await app.bootstrap()
    await asyncio.wait_for(entered.wait(), 3)
    try:
        await app.run_boot_self_change_check()
    finally:
        release.set()
    await app.next_reply()
    await app.wait_ledger_cleared()
    assert "detected at startup" in app.scripted.calls[0]["prompt"]
    assert len(app.scripted.calls) == 1


async def test_boot_reservation_survives_long_wait_for_player(core_app_factory, fast_self_change, tmp_plugin):
    app = await core_app_factory(turns=[ScriptedTurn(text="我还在这里。", when="A new session")])
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    state = read_self_change_state()
    state["delivery"]["in_flight"]["created_at"] = 0
    with sqlite3.connect(Path(mas_config.data_dir) / "muika.db") as conn:
        conn.execute("UPDATE system_state SET payload=? WHERE key='self_change'", (json.dumps(state),))
    await get_self_change_dispatcher().check()
    assert app.sent == []
    assert read_self_change_state()["delivery"]["in_flight"] is not None
    await app.bootstrap()
    await app.next_reply()
    await app.wait_ledger_cleared()
    assert len(app.scripted.calls) == 1


@pytest.mark.parametrize("outcome", ["failed", "exception", "cancelled", "fallback", "silent"])
async def test_boot_greeting_failure_retries_changes_without_repeating_greeting(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch, outcome
):
    from muika.core.brain import FALLBACK_REPLY

    first = FALLBACK_REPLY if outcome == "fallback" else "<do_nothing>" if outcome == "silent" else "欢迎回来。"
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text=first, when="A new session"),
            ScriptedTurn(text="醒来时发现自己有些变化。", when=SELF_CHANGED_WHEN),
        ]
    )
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    failed = asyncio.Event()
    send = app.muika.executor._send_func

    async def first_send_fails(content, resources=None, target=None):
        if not failed.is_set():
            failed.set()
            if outcome == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("transport unavailable")
        return await send(content, resources, target)

    if outcome == "failed":
        app.fail_next_sends(1)
    elif outcome in {"exception", "cancelled"}:
        monkeypatch.setattr(app.muika.executor, "_send_func", first_send_fails)
    await app.bootstrap()
    if outcome == "cancelled":
        await asyncio.wait_for(failed.wait(), 3)
        await app.muika.stop()
        await wait_for(lambda: read_self_change_state()["delivery"]["in_flight"]["next_attempt_at"] is not None)
        app.muika.start()
    if outcome != "silent":
        await wait_for(lambda: "醒来时发现自己有些变化。" in app.sent)
    await app.wait_ledger_cleared()
    assert sum("A new session" in call["prompt"] for call in app.scripted.calls) == 1
    if outcome != "silent":
        retry = app.scripted.calls[-1]["prompt"]
        assert "detected at startup" in retry and "A new session" not in retry
        assert "Let the greeting" not in retry and "before this greeting" not in retry
    else:
        assert len(app.scripted.calls) == 1 and app.sent == []
    assert self_change_notes(app) == []
    app.recorder.record("boot_receipt", outcome=outcome, pending=0, technical_experiences=0)


async def test_gateway_takeover_delivers_boot_changes_without_greeting(core_app_factory, fast_self_change, tmp_plugin):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="我在这里，也发现了一些变化。", when="activity location"),
            ScriptedTurn(text="欢迎回来。", when="A new session"),
        ]
    )
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    await app.muika.create_event(CoreChangeEvent("Active device: local"))
    await app.next_reply()
    await app.wait_ledger_cleared()
    prompt = app.scripted.calls[0]["prompt"]
    assert "detected at startup" in prompt
    assert "A new session" not in prompt and "Let the greeting" not in prompt
    assert len(app.scripted.calls) == 1
    assert self_change_notes(app) == []
    await app.bootstrap()
    await app.next_reply()
    assert "detected at startup" not in app.scripted.calls[1]["prompt"]
    app.recorder.record("gateway_boot_change", pending=0, repeated_changes=0)


@pytest.mark.parametrize("outcome", ["exception", "cancelled"])
async def test_boot_handoff_failure_releases_reservation(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch, outcome
):
    app = await core_app_factory(turns=[ScriptedTurn(text="醒来时发现自己有些变化。", when=SELF_CHANGED_WHEN)])
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    dispatcher = app.muika.self_change
    save = dispatcher.ledger._save_unlocked
    failed = asyncio.Event()

    async def first_save_fails(state):
        if not failed.is_set():
            failed.set()
            if outcome == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("ledger unavailable")
        return await save(state)

    monkeypatch.setattr(dispatcher.ledger, "_save_unlocked", first_save_fails)
    await app.bootstrap()
    await asyncio.wait_for(failed.wait(), 3)
    if outcome == "cancelled":
        await app.muika.stop()
        await wait_for(lambda: read_self_change_state()["delivery"]["in_flight"]["next_attempt_at"] is not None)
        app.muika.start()
    await wait_for(lambda: "醒来时发现自己有些变化。" in app.sent)
    await app.wait_ledger_cleared()
    assert dispatcher.boot_change is None
    assert len(app.scripted.calls) == 1
    prompt = app.scripted.calls[0]["prompt"]
    assert "detected at startup" in prompt and "A new session" not in prompt
    assert "Let the greeting" not in prompt
    assert self_change_notes(app) == []
    app.recorder.record("boot_handoff_recovered", outcome=outcome, pending=0)


async def test_queued_change_survives_process_restart(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app = await core_app_factory(turns=[ScriptedTurn(text="我注意到了。", when=SELF_CHANGED_WHEN)])
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    app.queue_next_sends(1)
    with plugin_file.open("a", encoding="utf-8") as stream:
        stream.write("\n# pending before restart\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")
    await app.wait_processed("self_changed")
    await wait_for(lambda: read_self_change_state()["delivery"]["in_flight"]["awaiting_flush"])
    await app.stop()
    restored = await core_app_factory(turns=[ScriptedTurn(text="欢迎回来，我还记得那点变化。", when="A new session")])
    await restored.start()
    restored.attach_self_change()
    await restored.run_boot_self_change_check()
    restored.muika.self_change.on_adapter_online()
    await asyncio.sleep(0.3)
    assert read_self_change_state()["pending"], "Old in-memory queue must not confirm a lost message"
    await restored.bootstrap()
    await restored.next_reply()
    await restored.wait_ledger_cleared()
    assert "detected at startup" in restored.scripted.calls[0]["prompt"]


async def test_runtime_revision_during_boot_greeting_is_delivered_later(
    core_app_factory, fast_self_change, tmp_plugin, monkeypatch
):
    _, plugin_file = tmp_plugin
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="欢迎回来。", when="A new session"),
            ScriptedTurn(text="这一次是刚才的变化。", when=SELF_CHANGED_WHEN),
        ]
    )
    await app.start()
    seed_boot_changes()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    entered, release = asyncio.Event(), asyncio.Event()
    send = app.muika.executor._send_func

    async def hold_first_send(content, resources=None, target=None):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await send(content, resources, target)

    monkeypatch.setattr(app.muika.executor, "_send_func", hold_first_send)
    await app.bootstrap()
    await asyncio.wait_for(entered.wait(), 3)
    try:
        snapshot = read_self_change_state()["delivery"]["in_flight"]["snapshot"]
        revision = next(entry["revision"] for entry in snapshot if entry["kind"] == "plugin_changed")
        with plugin_file.open("a", encoding="utf-8") as stream:
            stream.write("\n# a later revision\n")
        get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")
        await wait_for(lambda: any(entry["revision"] > revision for entry in read_self_change_state()["pending"]))
    finally:
        release.set()
    await wait_for(lambda: len(app.sent) == 2)
    await app.wait_ledger_cleared()
    assert "detected at startup" in app.scripted.calls[0]["prompt"]
    assert "detected at startup" not in app.scripted.calls[1]["prompt"]
    assert self_change_notes(app) == []


async def test_first_boot_builds_baseline_only(core_app_factory, fast_self_change):
    # 首次启动：只建基线，不产生任何感知
    app1 = await core_app_factory(turns=[])
    await app1.start()
    reset_self_change_state()
    app1.attach_self_change()
    await app1.run_boot_self_change_check()
    await wait_for(lambda: bool(read_self_change_state().get("fingerprints")), message="baseline not saved")
    await asyncio.sleep(0.3)
    assert app1.sent == []
    assert read_self_change_state()["pending"] == []
    await app1.stop()

    # 无变化重启：不通知
    app2 = await core_app_factory(turns=[])
    await app2.start()
    app2.attach_self_change()
    await app2.run_boot_self_change_check()
    await asyncio.sleep(0.3)
    assert app2.sent == []
    assert read_self_change_state()["pending"] == []


async def test_plugin_edit_noticed_once_and_not_after_restart(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app1 = await core_app_factory(turns=[ScriptedTurn(text="Ok.", when=SELF_CHANGED_WHEN)])
    await app1.start()
    reset_self_change_state()
    app1.attach_self_change()
    await app1.run_boot_self_change_check()

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# touched by master\n")
    assert get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime") is True

    reply = await app1.next_reply()
    assert_clean_visible(reply)
    await app1.wait_processed("self_changed")
    await app1.wait_ledger_cleared()
    # 请求侧事实：系统行给出了插件事实，模板给出了语域段落
    call = next(item for item in app1.scripted.calls if SELF_CHANGED_WHEN in item["prompt"])
    assert 'plugin "e2e-selfchg" was modified' in call["prompt"]
    assert "curiosity, not bureaucracy" in call["prompt"]
    assert "ticklish and intimate" in call["prompt"]  # edited 语域提示随事件注入
    assert read_self_change_state()["pending"] == []
    await app1.stop()

    # 基线已在运行时推进：重启后 boot diff 不应重复报告
    app2 = await core_app_factory(turns=[])
    await app2.start()
    app2.attach_self_change()
    await app2.run_boot_self_change_check()
    await asyncio.sleep(0.3)
    assert app2.sent == []
    assert read_self_change_state()["pending"] == []


async def test_version_upgrade_register_and_facts(core_app_factory, fast_self_change, monkeypatch):
    from muika.core.self_mod.fingerprint import compute_kernel_files

    # CI 浅克隆无 tags，安装版本号不可预测（如 0.0.1.dev1+...，会被判为降级）；
    # 注入确定版本，让"升级"语域判定不随安装环境漂移
    monkeypatch.setattr("muika.core.self_change.get_version", lambda: "1.2.3")
    app = await core_app_factory(
        turns=[ScriptedTurn(text="You upgraded me? What did you put in me this time?", when="A new session")]
    )
    await app.start()
    reset_self_change_state()
    # 预置旧版本基线：内核与当前一致，仅版本号不同
    seed = {
        "fingerprints": {"version": "0.9.9", "kernel": compute_kernel_files(), "user_plugins": {}},
        "pending": [],
        "delivery": {"last_delivered_at": None, "in_flight": None, "perceived_batches": 0},
    }
    db_path = Path(mas_config.data_dir) / "muika.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, payload, updated_at) VALUES ('self_change', ?, ?)",
            (json.dumps(seed), str(time.time())),
        )
        conn.commit()
    app.attach_self_change()
    await app.run_boot_self_change_check()

    await app.bootstrap()
    await app.next_reply()
    await app.wait_processed("session_bootstrap")
    await app.wait_ledger_cleared()
    call = next(item for item in app.scripted.calls if "A new session" in item["prompt"])
    assert "0.9.9" in call["prompt"] and "1.2.3" in call["prompt"]
    # 升级语域提示随事件注入 prompt
    assert "Your version was raised: 0.9.9 -> 1.2.3" in call["prompt"]


async def test_self_edit_advances_baseline_without_notice(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app1 = await core_app_factory(turns=[])
    await app1.start()
    reset_self_change_state()
    app1.attach_self_change()
    await app1.run_boot_self_change_check()

    # 她自己激活的变更：基线推进，账本不入账
    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# changed by herself\n")
    notify_plugin_change("e2e_selfchg_plugin", "self", "reload")
    await asyncio.sleep(0.4)
    assert app1.sent == []
    state = read_self_change_state()
    assert state["pending"] == []
    assert state["fingerprints"]["user_plugins"]["e2e_selfchg_plugin"]
    await app1.stop()

    app2 = await core_app_factory(turns=[])
    await app2.start()
    app2.attach_self_change()
    await app2.run_boot_self_change_check()
    await asyncio.sleep(0.3)
    assert app2.sent == []
    assert read_self_change_state()["pending"] == []


async def test_command_reload_writes_note_without_notice(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app1 = await core_app_factory(turns=[])
    await app1.start()
    reset_self_change_state()
    app1.attach_self_change()
    await app1.run_boot_self_change_check()

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# reloaded by command\n")
    assert get_plugin_manager().reload("e2e_selfchg_plugin") is True  # 默认 origin=command
    await asyncio.sleep(0.4)
    assert app1.sent == []
    assert read_self_change_state()["pending"] == []
    await app1.stop()

    # 简短事实入记忆，供她日后想起；重启后也不重复报告
    app2 = await core_app_factory(turns=[])
    await app2.start()
    notes = app2.db_query("SELECT source FROM experience WHERE source LIKE 'plugin-reload:%'")
    assert len(notes) == 1
    app2.attach_self_change()
    await app2.run_boot_self_change_check()
    await asyncio.sleep(0.3)
    assert app2.sent == []
    assert read_self_change_state()["pending"] == []


async def test_send_failure_retries_without_technical_memory(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="I can feel it... you changed my plugin.", when=SELF_CHANGED_WHEN),
            ScriptedTurn(text="There you are again, my new self.", when=SELF_CHANGED_WHEN),
        ]
    )
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    app.fail_next_sends(1)

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# first edit\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")

    reply = await app.next_reply()
    assert_clean_visible(reply)
    await app.wait_processed("self_changed")
    await app.wait_ledger_cleared()
    assert len(app.sent) == 1
    notes = self_change_notes(app)
    assert notes == []  # 投递重试不复制技术账本。


async def test_batch_snapshot_keeps_changes_arriving_during_flight(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app = await core_app_factory(
        turns=[
            ScriptedTurn(text="First notice.", when=SELF_CHANGED_WHEN),
            ScriptedTurn(text="And again?", when=SELF_CHANGED_WHEN),
        ]
    )
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# edit one\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")
    await wait_for(lambda: bool(read_self_change_state()["pending"]), message="entry not recorded")

    # 冻结批次后同一插件再次变更：修订号推进，销账只消费快照内的修订
    dispatcher = get_self_change_dispatcher()
    batch_id, snapshot = await dispatcher.ledger.freeze_batch()  # type: ignore[union-attr]
    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# edit two\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")
    await wait_for(
        lambda: read_self_change_state()["pending"]
        and read_self_change_state()["pending"][0]["revision"] > snapshot[0]["revision"],
        message="revision not advanced",
    )
    await dispatcher.ledger.resolve_batch(batch_id)
    await wait_for(lambda: bool(read_self_change_state()["pending"]), message="later change was dropped")

    # 之后追加的变化在下一轮投递中送达
    reply = await app.next_reply()
    assert_clean_visible(reply)
    await app.wait_ledger_cleared()
    assert len(app.sent) == 1  # 批次一在冻结后未走事件管线，只有批次二实际发言


async def test_do_nothing_consumes_batch_silently(core_app_factory, fast_self_change, tmp_plugin):
    _, plugin_file = tmp_plugin
    app = await core_app_factory(turns=[ScriptedTurn(text="<do_nothing>", when=SELF_CHANGED_WHEN)])
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# noticed quietly\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")

    await app.wait_processed("self_changed")
    await app.wait_ledger_cleared()
    assert app.sent == []
    state = read_self_change_state()
    assert state["pending"] == []
    assert state["delivery"]["perceived_batches"] == 1  # 沉默也算已感知，不反复催她开口


async def test_maintenance_blocks_delivery_until_over(core_app_factory, fast_self_change, tmp_plugin, monkeypatch):
    _, plugin_file = tmp_plugin
    import muika.core.self_change as self_change_module

    app = await core_app_factory(turns=[ScriptedTurn(text="Oh, the dust settles.", when=SELF_CHANGED_WHEN)])
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()

    monkeypatch.setattr(self_change_module, "is_core_maintenance_active", lambda: True)
    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# during maintenance\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")
    await asyncio.sleep(0.5)
    assert app.sent == []
    assert read_self_change_state()["pending"]

    # 维护结束：恢复投递
    monkeypatch.setattr(self_change_module, "is_core_maintenance_active", lambda: False)
    reply = await app.next_reply()
    assert_clean_visible(reply)
    await app.wait_ledger_cleared()


async def test_plugin_inspect_tool_executes_with_real_result(core_app_factory, fast_self_change, tmp_plugin):
    app = await core_app_factory(
        turns=[
            ScriptedTurn(
                text="",
                when="What plugins",
                tool_calls=(ToolCall(id="call-1", name="plugin_inspect", arguments="{}"),),
            ),
            ScriptedTurn(text="I see it now.", when=None),  # 工具回传后的续写请求不带原句
        ]
    )
    await app.start()

    await app.user_says("What plugins do I have right now?")
    reply = await app.next_reply()
    assert reply == "I see it now."
    tool_calls = [call for call in app.scripted.calls if call["tool_calls"] == ["plugin_inspect"]]
    assert tool_calls, "plugin_inspect was never requested"
    tool_exec = next(
        entry for entry in app.recorder.entries if entry["kind"] == "tool_exec" and entry["name"] == "plugin_inspect"
    )
    assert not tool_exec["is_error"]
    assert "e2e-selfchg" in tool_exec["result"]


async def test_queued_receipt_parks_until_adapter_online(core_app_factory, fast_self_change, tmp_plugin):
    """queued 回执不清账：停靠等待适配器上线补发，避免重复发言也不丢账。"""
    _, plugin_file = tmp_plugin
    app = await core_app_factory(turns=[ScriptedTurn(text="I noticed... I think.", when=SELF_CHANGED_WHEN)])
    await app.start()
    reset_self_change_state()
    app.attach_self_change()
    await app.run_boot_self_change_check()
    app.queue_next_sends(1)

    with plugin_file.open("a", encoding="utf-8") as handle:
        handle.write("\n# queued edit\n")
    get_plugin_manager().reload("e2e_selfchg_plugin", origin="runtime")

    await app.wait_processed("self_changed")
    await asyncio.sleep(0.2)
    state = read_self_change_state()
    assert state["pending"], "queued 后账目被清空，进程退出将丢失感知"
    assert state["delivery"]["in_flight"]["awaiting_flush"] is True

    # 适配器上线：暂存队列补发，账本确认（不再重投，避免重复发言）
    app.muika.self_change.on_adapter_online()
    await app.wait_ledger_cleared()
    assert read_self_change_state()["pending"] == []
    assert self_change_notes(app) == []
    dispatches = [call for call in app.scripted.calls if call["prompt"].count(SELF_CHANGED_WHEN)]
    assert len(dispatches) == 1


async def test_self_change_exemption_verifies_content(core_app_factory, fast_self_change, monkeypatch):
    """自改豁免必须核对内容：旧的重启记录不能屏蔽其后的外部修改。"""
    kernel_root_path = Path(muika.__file__).resolve().parent
    target = kernel_root_path / "agreement.py"
    original = target.read_bytes()
    proposal_file = Path(mas_config.data_dir) / "test_proposal.json"

    def sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def make_record(status: str, expected: str) -> dict:
        proposal_file.write_text(
            json.dumps({"changes": [{"path": "muika/agreement.py", "action": "modify", "sha256_after": expected}]}),
            encoding="utf-8",
        )
        return {
            "id": "restart-1",
            "patch_id": "00000000_000000_abc12345",
            "status": status,
            "proposal_file": str(proposal_file),
        }

    def clear_pending_keep_baseline() -> None:
        state = read_self_change_state()
        state["pending"] = []
        db_path = Path(mas_config.data_dir) / "muika.db"
        with sqlite3.connect(db_path) as conn:
            conn.execute("UPDATE system_state SET payload=? WHERE key='self_change'", (json.dumps(state),))
            conn.commit()

    # can_send=False：本场景只验证检测与豁免，不让批次真正投递
    app = await core_app_factory(turns=[])
    await app.start()
    reset_self_change_state()
    app.attach_self_change(can_send=False)
    await app.run_boot_self_change_check()
    try:
        # 她自改了 agreement.py，提案预期 = 磁盘现状 → 豁免且基线推进
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# her own edit\n")
        modified = target.read_text(encoding="utf-8")
        await app.run_boot_self_change_check(make_record("started", sha256_text(modified)))
        assert read_self_change_state()["pending"] == []

        # 玩家随后又改了同一文件：旧提案的 sha256_after 不再匹配 → 外部入账
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# master's later edit\n")
        await app.run_boot_self_change_check(make_record("started", sha256_text(modified)))
        assert [entry["target"] for entry in read_self_change_state()["pending"]] == ["agreement.py"]

        # 状态不是 started（提案未真正应用）：即使内容与提案一致也不豁免
        clear_pending_keep_baseline()
        with target.open("a", encoding="utf-8") as handle:
            handle.write("\n# another edit, proposal claims this\n")
        claimed = target.read_text(encoding="utf-8")
        await app.run_boot_self_change_check(make_record("failed", sha256_text(claimed)))
        assert [entry["target"] for entry in read_self_change_state()["pending"]] == ["agreement.py"]
    finally:
        target.write_bytes(original)
        proposal_file.unlink(missing_ok=True)


async def test_builtin_plugins_excluded_from_plugin_baseline(core_app_factory, fast_self_change, tmp_plugin):
    """builtin 插件属于内核：不得进入插件基线，避免与内核 diff 重复报告。"""
    from muika.plugin.loader import load_plugins, unload_plugin

    package_dir = Path(muika.__file__).resolve().parent
    loaded = load_plugins(package_dir / "builtin_plugins", base_path=package_dir.parent)
    try:
        _, plugin_file = tmp_plugin
        app = await core_app_factory(turns=[])
        await app.start()
        reset_self_change_state()
        app.attach_self_change()
        await app.run_boot_self_change_check()
        baseline = read_self_change_state()["fingerprints"]["user_plugins"]
        assert "e2e_selfchg_plugin" in baseline
        assert not [name for name in baseline if name.startswith("muika.builtin_plugins")]
    finally:
        for plugin in loaded:
            unload_plugin(plugin.package_name)


async def test_report_distinguishes_kernel_files_from_plugins(core_app_factory, fast_self_change, tmp_plugin):
    """kernel_file 不得被描述成插件，内核清单折叠后不再逐项展开。"""
    _, plugin_file = tmp_plugin
    app = await core_app_factory(turns=[ScriptedTurn(text="Core and plugin, both changed.", when=SELF_CHANGED_WHEN)])
    await app.start()
    reset_self_change_state()
    now = time.time()
    seed = {
        "fingerprints": {"version": "1.2.3", "kernel": {"core/brain.py": "d" * 64}, "user_plugins": {}},
        "pending": [
            {
                "key": "kernel_file:core/brain.py",
                "kind": "kernel_file",
                "target": "core/brain.py",
                "origins": ["boot"],
                "before": None,
                "after": {"digest": "e" * 64},
                "plugin_meta": None,
                "version_from": None,
                "version_to": None,
                "first_ts": now,
                "last_ts": now,
                "observations": 1,
                "revision": 1,
                "restart_id": None,
            },
            {
                "key": "plugin_changed:e2e_selfchg_plugin",
                "kind": "plugin_changed",
                "target": "e2e_selfchg_plugin",
                "origins": ["runtime"],
                "before": {"digest": "a" * 64},
                "after": {"digest": "b" * 64},
                "plugin_meta": {"name": "e2e-selfchg", "description": "test"},
                "version_from": None,
                "version_to": None,
                "first_ts": now,
                "last_ts": now,
                "observations": 1,
                "revision": 2,
                "restart_id": None,
            },
        ],
        "delivery": {"last_delivered_at": None, "in_flight": None, "perceived_batches": 0},
    }
    db_path = Path(mas_config.data_dir) / "muika.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO system_state (key, payload, updated_at) VALUES ('self_change', ?, ?)",
            (json.dumps(seed), str(now)),
        )
        conn.commit()
    app.attach_self_change()

    reply = await app.next_reply()
    assert_clean_visible(reply)
    call = next(item for item in app.scripted.calls if SELF_CHANGED_WHEN in item["prompt"])
    assert "Core files changed: 1." in call["prompt"]
    assert "core/brain.py" not in call["prompt"]
    assert 'Your plugin "e2e-selfchg" was modified.' in call["prompt"]
    assert 'plugin "py"' not in call["prompt"]
    # 语域：既有内核（boot）又有插件（runtime），无版本变化 → 被实时修改
    assert "Changes were observed during this run." in call["prompt"]
