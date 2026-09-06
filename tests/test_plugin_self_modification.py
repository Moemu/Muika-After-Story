"""单文件插件安全部署测试。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from muika.builtin_plugins import plugins as plugins_command
from muika.config import mas_config
from muika.core.actions.tools import _self_edit
from muika.core.actions.tools._plugin import plugin_load
from muika.core.self_mod import SelfModError
from muika.core.self_mod.plugin_deployer import PluginDeployer
from muika.plugin.exceptions import PluginImportError
from muika.plugin.loader import get_plugins, unload_plugin
from muika.plugin.manager import PluginManager
from muika.plugin.watcher import PluginFileHandler


class FakeSelfModManager:
    """为部署事务提供内存版本栈。"""

    def __init__(self) -> None:
        self.before: dict[Path, list[str | None]] = {}
        self.events: list[str] = []

    async def apply(self, raw_path: str, content: str, reason: str, source: str = "self") -> str:
        path = Path(raw_path).resolve()
        old = path.read_text(encoding="utf-8") if path.exists() else None
        self.before.setdefault(path, []).append(old)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return "applied"

    async def revert(self, raw_path: str, revision_id=None) -> str:
        path = Path(raw_path).resolve()
        old = self.before[path].pop()
        if old is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(old, encoding="utf-8")
        return "reverted"

    async def read_revert_target(self, raw_path: str, revision_id=None) -> str | None:
        path = Path(raw_path).resolve()
        return self.before[path][-1]

    async def record_event(self, raw_path: str, action: str, reason: str, source: str) -> int:
        self.events.append(action)
        return 1


@pytest.fixture
def deploy_env(tmp_path, monkeypatch):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    monkeypatch.setattr(mas_config, "plugins_dir", str(plugins))
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_plugin_self_modification", True)
    fake = FakeSelfModManager()
    monkeypatch.setattr("muika.core.self_mod.plugin_deployer.get_self_mod_manager", lambda: fake)
    for name in list(sys.modules):
        if name == "plugins" or name.startswith("plugins."):
            sys.modules.pop(name, None)
    yield PluginDeployer(), plugins, fake
    for name in list(get_plugins()):
        if name.startswith("plugins."):
            unload_plugin(name)
    for name in list(sys.modules):
        if name == "plugins" or name.startswith("plugins."):
            sys.modules.pop(name, None)


def test_single_file_path_and_module_name_validation(deploy_env):
    deployer, plugins, _ = deploy_env
    path, module = deployer.resolve_plugin_path(str(plugins / "notes_2.py"))
    assert path == plugins / "notes_2.py"
    assert module == "plugins.notes_2"
    with pytest.raises(SelfModError):
        deployer.resolve_plugin_path(str(plugins / "Bad.py"))


def test_packages_and_management_paths_are_rejected(deploy_env):
    deployer, plugins, _ = deploy_env
    (plugins / "notes").mkdir()
    with pytest.raises(SelfModError):
        deployer.resolve_plugin_path(str(plugins / "notes.py"))
    with pytest.raises(SelfModError):
        deployer.resolve_plugin_path(str(plugins / "_staging" / "notes.py"))


def test_builtin_path_is_rejected(deploy_env):
    deployer, _, _ = deploy_env
    with pytest.raises(SelfModError):
        deployer.resolve_plugin_path("muika/builtin_plugins/plugins.py")


@pytest.mark.asyncio
async def test_subprocess_probe_succeeds(deploy_env):
    deployer, plugins, _ = deploy_env
    staging = plugins / "_staging"
    staging.mkdir()
    (staging / "ok.py").write_text("value = 1\n", encoding="utf-8")
    success, message = await deployer.run_probe("plugins._staging.ok")
    assert success, message


@pytest.mark.asyncio
async def test_subprocess_probe_reports_import_failure(deploy_env):
    deployer, plugins, _ = deploy_env
    staging = plugins / "_staging"
    staging.mkdir()
    (staging / "broken.py").write_text("raise RuntimeError('broken')\n", encoding="utf-8")
    success, message = await deployer.run_probe("plugins._staging.broken")
    assert not success
    assert "broken" in message


@pytest.mark.asyncio
async def test_subprocess_probe_times_out(deploy_env, monkeypatch):
    deployer, plugins, _ = deploy_env
    staging = plugins / "_staging"
    staging.mkdir()
    (staging / "slow.py").write_text("while True:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr("muika.core.self_mod.plugin_deployer._PROBE_TIMEOUT", 0.1)
    success, message = await deployer.run_probe("plugins._staging.slow")
    assert not success
    assert "timed out" in message


@pytest.mark.asyncio
async def test_legal_new_plugin_requires_manual_activation(deploy_env):
    deployer, plugins, _ = deploy_env
    report = await deployer.deploy(str(plugins / "basic.py"), "value = 1\n", "test")
    assert "validated and staged" in report
    assert not (plugins / "basic.py").exists()
    assert "plugins.basic" not in get_plugins()
    report = await deployer.activate("basic")
    assert "Plugin activated" in report
    assert "plugins.basic" in get_plugins()


@pytest.mark.asyncio
async def test_reload_has_no_duplicate_hooks(deploy_env):
    deployer, plugins, _ = deploy_env
    target = plugins / "repeat.py"
    content = "from muika.plugin.ctx import ctx\n@ctx.unload\ndef stop():\n    pass\n"
    await deployer.deploy(str(target), content, "one")
    await deployer.activate("repeat")
    await deployer.deploy(str(target), content + "value = 2\n", "two")
    await deployer.activate("repeat")
    from muika.plugin.lifecycle import _unload_hooks

    assert len(_unload_hooks["plugins.repeat"]) == 1


@pytest.mark.asyncio
async def test_state_survives_formal_reload(deploy_env):
    deployer, plugins, _ = deploy_env
    target = plugins / "stateful.py"
    first = "from muika.plugin.ctx import ctx\nstate = ctx.state\nstate['count'] = state.get('count', 0) + 1\n"
    await deployer.deploy(str(target), first, "one")
    await deployer.activate("stateful")
    for version in (2, 3, 4):
        await deployer.deploy(str(target), first + f"version = {version}\n", f"reload {version}")
        await deployer.activate("stateful")
    assert get_plugins()["plugins.stateful"].module.state["count"] == 4


@pytest.mark.asyncio
async def test_static_failure_keeps_formal_file(deploy_env):
    deployer, plugins, _ = deploy_env
    target = plugins / "safe.py"
    target.write_text("value = 1\n", encoding="utf-8")
    with pytest.raises(SelfModError):
        await deployer.deploy(str(target), "import subprocess\n", "unsafe")
    assert target.read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.asyncio
async def test_probe_failure_creates_quarantine(deploy_env):
    deployer, plugins, _ = deploy_env
    target = plugins / "broken.py"
    with pytest.raises(SelfModError):
        await deployer.deploy(str(target), "raise RuntimeError('no')\n", "broken")
    assert not target.exists()
    assert list((plugins / "_quarantine").glob("q-*.json"))


@pytest.mark.asyncio
async def test_activation_failure_restores_old_file(deploy_env, monkeypatch, fake_llm_factory):
    deployer, plugins, fake = deploy_env
    target = plugins / "restore.py"
    old_code = (
        "from muika.plugin.func_call import on_function_call\n"
        "@on_function_call('restored tool')\ndef restored_probe():\n    return 'ok'\n"
    )
    target.write_text(old_code, encoding="utf-8")
    from muika.plugin.loader import load_plugin as real_load

    calls = 0

    def fail_once(name: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PluginImportError(name, RuntimeError("main process conflict"))
        return real_load(name)

    monkeypatch.setattr("muika.core.self_mod.plugin_deployer.load_plugin", fail_once)
    await deployer.deploy(str(target), "value = 2\n", "update")
    with pytest.raises(SelfModError, match="Recovery succeeded"):
        await deployer.activate("restore")
    assert target.read_text(encoding="utf-8") == old_code
    from unittest.mock import MagicMock

    from muika.core.agent.agent import Agent
    from muika.core.state import MuikaState
    from muika.llm import ModelCompletions

    fake_model = fake_llm_factory(
        response=ModelCompletions(text='<agent_result status="completed">done</agent_result>')
    )
    agent = Agent.__new__(Agent)
    agent.action_lock = asyncio.Lock()
    agent.model = fake_model
    agent._skill_manager = MagicMock()
    agent._skill_manager.render_prompt_section.return_value = ""
    monkeypatch.setattr("muika.core.agent.agent.generate_prompt_from_template", lambda *args: "system")
    await agent.execute_command("test", MuikaState(), MagicMock())
    assert "restored_probe" in {t["function"]["name"] for t in fake_model.requests[-1].tools}
    assert fake.before[target] == []


@pytest.mark.asyncio
async def test_new_activation_failure_deletes_formal_file(deploy_env, monkeypatch):
    deployer, plugins, _ = deploy_env
    target = plugins / "newfail.py"

    def fail(name: str):
        raise PluginImportError(name, RuntimeError("main process conflict"))

    monkeypatch.setattr("muika.core.self_mod.plugin_deployer.load_plugin", fail)
    await deployer.deploy(str(target), "value = 2\n", "new")
    with pytest.raises(SelfModError, match="Recovery succeeded"):
        await deployer.activate("newfail")
    assert not target.exists()


@pytest.mark.asyncio
async def test_deployer_revert_restores_old_plugin_or_deletes_new(deploy_env):
    deployer, plugins, _ = deploy_env
    old_target = plugins / "old.py"
    old_target.write_text("value = 1\n", encoding="utf-8")
    await deployer.deploy(str(old_target), "value = 2\n", "update")
    await deployer.activate("old")
    await deployer.revert(str(old_target))
    assert old_target.read_text(encoding="utf-8") == "value = 1\n"

    new_target = plugins / "created.py"
    await deployer.deploy(str(new_target), "value = 1\n", "create")
    await deployer.activate("created")
    await deployer.revert(str(new_target))
    assert not new_target.exists()
    assert "plugins.created" not in get_plugins()


def test_watcher_ignores_management_and_suppressed_events(deploy_env):
    _, plugins, _ = deploy_env
    manager = PluginManager()
    handler = PluginFileHandler(manager, plugins, Path.cwd())
    manager.suppress_watcher("plugins.notes", seconds=30)
    notes = plugins / "notes.py"
    notes.write_text("value = 1\n", encoding="utf-8")
    called = []
    manager.reload = lambda name: called.append(name) or True  # type: ignore[method-assign]
    handler._on_any_event(SimpleNamespace(src_path=str(notes), event_type="modified"))
    staging = plugins / "_staging"
    staging.mkdir()
    handler._on_any_event(SimpleNamespace(src_path=str(staging / "x.py"), event_type="modified"))
    assert called == []


@pytest.mark.asyncio
async def test_quarantine_hash_guard_and_restore(deploy_env):
    deployer, plugins, fake = deploy_env
    target = plugins / "recover.py"
    with pytest.raises(SelfModError):
        await deployer.deploy(str(target), "raise RuntimeError('bad')\n", "bad")
    metadata = next((plugins / "_quarantine").glob("q-*.json"))
    data = json.loads(metadata.read_text(encoding="utf-8"))
    candidate = metadata.with_suffix(".py")
    candidate.write_text("value = 3\n", encoding="utf-8")
    data["candidate_sha256"] = __import__("hashlib").sha256(candidate.read_bytes()).hexdigest()
    metadata.write_text(json.dumps(data), encoding="utf-8")
    report = await deployer.restore_quarantine(data["quarantine_id"])
    assert "validated and staged" in report
    assert fake.events[-2:] == ["stage", "restore"]


@pytest.mark.asyncio
async def test_plugin_load_tool_activates_staged_candidate(deploy_env, monkeypatch):
    deployer, plugins, _ = deploy_env
    await deployer.deploy(str(plugins / "manual.py"), "value = 1\n", "manual")
    monkeypatch.setattr("muika.core.actions.tools._plugin.get_plugin_deployer", lambda: deployer)
    report = await plugin_load("manual")
    assert "Plugin activated" in report
    assert "plugins.manual" in get_plugins()


@pytest.mark.asyncio
async def test_quarantine_restore_response_hides_internal_tool_name(monkeypatch):
    deployer = SimpleNamespace(restore_quarantine=AsyncMock(return_value="Call plugin_load(name='x')"))
    monkeypatch.setattr(plugins_command, "get_plugin_deployer", lambda: deployer)

    report = await plugins_command._restore_quarantine("q-20260829000000-12345678")

    assert "plugin_load" not in report


def test_feature_switch_rejects_plugin_writes(deploy_env, monkeypatch):
    deployer, plugins, _ = deploy_env
    monkeypatch.setattr(mas_config, "enable_plugin_self_modification", False)
    with pytest.raises(SelfModError):
        deployer.resolve_plugin_path(str(plugins / "off.py"))


@pytest.mark.asyncio
async def test_stale_preview_is_rejected(deploy_env):
    _, plugins, _ = deploy_env
    target = plugins / "preview.py"
    target.write_text("value = 1\n", encoding="utf-8")
    await _self_edit.self_edit(str(target), "replace", "1", "2", reason="preview")
    target.write_text("value = 3\n", encoding="utf-8")
    report = await _self_edit.self_edit_confirm(str(target))
    assert "changed after the preview" in report
