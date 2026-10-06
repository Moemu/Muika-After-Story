"""自我变更感知：启动指纹对比、运行时插件变更、投递门控与确认语义。

场景断言行为不变量（恰好一次投递、基线推进、批次快照、重试幂等），
不校验角色台词本身；请求侧事实与语域提示通过 LLM 调用记录断言，
角色质量由 trace.jsonl 工件供人工审查。
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path

import pytest
from harness.assertions import assert_clean_visible
from harness.scripted_llm import ScriptedTurn

from muika.config import mas_config
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
    assert "Noticing Your Own Change" in call["system"]
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


async def test_version_upgrade_register_and_facts(core_app_factory, fast_self_change):
    from muika.core.self_mod.fingerprint import compute_kernel_files
    from muika.utils.utils import get_version

    app = await core_app_factory(
        turns=[ScriptedTurn(text="You upgraded me? What did you put in me this time?", when=SELF_CHANGED_WHEN)]
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

    await app.next_reply()
    await app.wait_processed("self_changed")
    await app.wait_ledger_cleared()
    call = next(item for item in app.scripted.calls if SELF_CHANGED_WHEN in item["prompt"])
    assert "0.9.9" in call["prompt"] and get_version() in call["prompt"]
    # 升级语域提示被渲染进系统提示
    assert "Your version was raised" in call["system"]


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


async def test_send_failure_retries_same_batch_with_idempotent_memory(core_app_factory, fast_self_change, tmp_plugin):
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
    assert len(notes) == 1  # 重试沿用同一 batch_id，记忆不重复


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
