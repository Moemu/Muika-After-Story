"""插件生命周期测试：ownership 追踪、unload/reload、Butler.refresh_tools、.plugins 命令、PluginWatcher 推导。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from muika.plugin import loader as loader_mod
from muika.plugin.command import (
    _commands,
    on_alconna,
    remove_commands_for_plugin,
)
from muika.plugin.func_call.caller import (
    Caller,
    _caller_data,
    on_function_call,
    remove_callers_for_plugin,
)
from muika.plugin.loader import (
    _declared_plugins,
    _loading_plugin,
    _plugins,
    load_plugin,
    reload_plugin,
    unload_plugin,
)
from muika.plugin.manager import _BUILTIN_PREFIX, PluginManager

# --------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolate_registries():
    """每个测试前后保存并恢复 _commands / _caller_data / _plugins / _declared_plugins。"""
    saved_commands = list(_commands)
    saved_callers = dict(_caller_data)
    saved_plugins = dict(_plugins)
    saved_declared = set(_declared_plugins)
    yield
    _commands[:] = saved_commands
    _caller_data.clear()
    _caller_data.update(saved_callers)
    _plugins.clear()
    _plugins.update(saved_plugins)
    _declared_plugins.clear()
    _declared_plugins.update(saved_declared)


# --------------------------------------------------------------------------- ownership tracking


def test_loading_plugin_contextvar_tags_command_registry():
    """load_plugin 设置 _loading_plugin 后，on_alconna 自动写入 plugin_package。"""
    token = _loading_plugin.set("plugins.test_plugin")
    try:
        from arclet.alconna import Alconna

        on_alconna(Alconna("testcmd_xyz"))
    finally:
        _loading_plugin.reset(token)

    match = [c for c in _commands if c.alc.command == "testcmd_xyz"]
    assert len(match) == 1
    assert match[0].plugin_package == "plugins.test_plugin"


def test_loading_plugin_contextvar_tags_caller():
    """load_plugin 设置 _loading_plugin 后，on_function_call 自动写入 plugin_package。"""
    token = _loading_plugin.set("plugins.test_plugin")
    try:

        @on_function_call(description="test func")
        def my_test_func_xyz():  # noqa: D401
            return "ok"

    finally:
        _loading_plugin.reset(token)

    assert "my_test_func_xyz" in _caller_data
    assert _caller_data["my_test_func_xyz"].plugin_package == "plugins.test_plugin"


def test_registration_outside_load_has_no_owner():
    """不在 load_plugin 上下文中的注册，plugin_package 应为 None。"""
    from arclet.alconna import Alconna

    on_alconna(Alconna("orphan_cmd"))
    match = [c for c in _commands if c.alc.command == "orphan_cmd"]
    assert match[0].plugin_package is None


# --------------------------------------------------------------------------- remove_* helpers


def test_remove_commands_for_plugin_filters_only_that_plugin():
    from arclet.alconna import Alconna

    r1 = on_alconna(Alconna("cmd_a"))
    r1.plugin_package = "plugins.foo"
    r2 = on_alconna(Alconna("cmd_b"))
    r2.plugin_package = "plugins.bar"
    r3 = on_alconna(Alconna("cmd_c"))
    r3.plugin_package = "plugins.foo"

    removed = remove_commands_for_plugin("plugins.foo")
    assert removed == 2
    remaining = [c.alc.command for c in _commands]
    assert "cmd_b" in remaining
    assert "cmd_a" not in remaining
    assert "cmd_c" not in remaining


def test_remove_callers_for_plugin_filters_only_that_plugin():
    c1 = Caller("d1")
    c1.plugin_package = "plugins.foo"
    c1._name = "f1"
    _caller_data["f1"] = c1
    c2 = Caller("d2")
    c2.plugin_package = "plugins.bar"
    c2._name = "f2"
    _caller_data["f2"] = c2

    removed = remove_callers_for_plugin("plugins.foo")
    assert removed == 1
    assert "f2" in _caller_data
    assert "f1" not in _caller_data


# --------------------------------------------------------------------------- unload


def test_unload_plugin_removes_all_registrations_and_sys_modules():
    """unload_plugin 应从 _plugins / _declared_plugins / _commands / _caller_data / sys.modules 全部清理。"""
    # 准备一个虚拟插件模块
    fake_mod = types.ModuleType("plugins.fake_unload_test")
    fake_mod.__path__ = []  # 标记为 package
    sys.modules["plugins.fake_unload_test"] = fake_mod
    sys.modules["plugins.fake_unload_test.sub"] = types.ModuleType("plugins.fake_unload_test.sub")

    plugin = loader_mod.Plugin(name="fake", module=fake_mod, package_name="plugins.fake_unload_test", meta=None)
    _plugins["plugins.fake_unload_test"] = plugin
    _declared_plugins.add("plugins.fake_unload_test")

    # 注册一个 command 和一个 func_call，标记为此插件
    from arclet.alconna import Alconna

    r = on_alconna(Alconna("fake_cmd"))
    r.plugin_package = "plugins.fake_unload_test"
    c = Caller("fake_desc")
    c.plugin_package = "plugins.fake_unload_test"
    c._name = "fake_func"
    _caller_data["fake_func"] = c

    ok = unload_plugin("plugins.fake_unload_test")
    assert ok is True
    assert "plugins.fake_unload_test" not in _plugins
    assert "plugins.fake_unload_test" not in _declared_plugins
    assert "plugins.fake_unload_test" not in sys.modules
    assert "plugins.fake_unload_test.sub" not in sys.modules
    assert all(c.alc.command != "fake_cmd" for c in _commands)
    assert "fake_func" not in _caller_data


def test_unload_nonexistent_returns_false():
    assert unload_plugin("plugins.does.not.exist") is False


# --------------------------------------------------------------------------- PluginManager


class FakeButler:
    def __init__(self) -> None:
        self.tools: list = []
        self._mcp_tools: list = []
        self.refresh_count = 0

    def refresh_tools(self) -> int:
        self.refresh_count += 1
        return len(self.tools)


def test_plugin_manager_refuses_builtin_unload():
    mgr = PluginManager(butler=FakeButler())
    assert mgr.unload("muika.builtin_plugins.reflect") is False
    assert mgr.reload("muika.builtin_plugins.reflect") is False


def test_plugin_manager_refresh_butler_calls_butler():
    butler = FakeButler()
    mgr = PluginManager(butler=butler)
    mgr.refresh_butler()
    assert butler.refresh_count == 1


def test_plugin_manager_refresh_butler_no_butler_returns_zero():
    mgr = PluginManager()
    assert mgr.refresh_butler() == 0


def test_plugin_manager_list_loaded_includes_counts():
    from arclet.alconna import Alconna

    plugin = loader_mod.Plugin(
        name="list_test",
        module=types.ModuleType("plugins.list_test"),
        package_name="plugins.list_test",
        meta=None,
    )
    _plugins["plugins.list_test"] = plugin
    r = on_alconna(Alconna("listcmd"))
    r.plugin_package = "plugins.list_test"
    c = Caller("list_desc")
    c.plugin_package = "plugins.list_test"
    c._name = "list_func"
    _caller_data["list_func"] = c

    mgr = PluginManager()
    info = mgr.list_loaded()["plugins.list_test"]
    assert info["name"] == "list_test"
    assert info["commands"] == 1
    assert info["func_calls"] == 1
    assert info["is_builtin"] is False


# --------------------------------------------------------------------------- 失败加载回滚


def test_failed_load_does_not_poison_declared_plugins(monkeypatch):
    """加载失败后 _declared_plugins 应回滚，允许修复后重新加载（热重载核心场景）。"""

    def raiser(name):
        raise RuntimeError("boom")

    real_import = importlib.import_module
    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    assert load_plugin("plugins.broken_probe") is None
    assert "plugins.broken_probe" not in _declared_plugins

    # 模拟修复后重试：导入成功
    monkeypatch.setattr(loader_mod.importlib, "import_module", real_import)
    sys.modules["plugins.broken_probe"] = types.ModuleType("plugins.broken_probe")
    try:
        assert load_plugin("plugins.broken_probe") is not None
    finally:
        unload_plugin("plugins.broken_probe")


def test_failed_load_cleans_partial_registrations(monkeypatch):
    """import 半途失败时，已注册的 commands 应一并清理，不留孤儿注册。"""

    def raiser(name):
        from arclet.alconna import Alconna

        on_alconna(Alconna("partial_probe_cmd"))  # 模拟模块执行半途的注册
        raise RuntimeError("boom")

    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    assert load_plugin("plugins.partial_probe") is None
    assert all(c.alc.command != "partial_probe_cmd" for c in _commands)
    assert "plugins.partial_probe" not in _declared_plugins


# --------------------------------------------------------------------------- reload 语义


def test_reload_plugin_loads_never_loaded_plugin():
    """reload_plugin 对从未加载过的新插件应直接加载（watcher 捡新文件的路径）。"""
    sys.modules["plugins.fresh_probe"] = types.ModuleType("plugins.fresh_probe")
    try:
        plugin = reload_plugin("plugins.fresh_probe")
        assert plugin is not None
        assert plugin.package_name == "plugins.fresh_probe"
        assert "plugins.fresh_probe" in _plugins
    finally:
        unload_plugin("plugins.fresh_probe")
    assert "plugins.fresh_probe" not in sys.modules


def test_manager_unload_refreshes_butler():
    """PluginManager.unload 成功后应刷新 Butler 工具列表，避免 LLM 继续看到死工具。"""
    butler = FakeButler()
    mgr = PluginManager(butler=butler)
    _plugins["plugins.unload_refresh"] = loader_mod.Plugin(
        name="unload_refresh",
        module=types.ModuleType("plugins.unload_refresh"),
        package_name="plugins.unload_refresh",
        meta=None,
    )
    assert mgr.unload("plugins.unload_refresh") is True
    assert butler.refresh_count == 1


# --------------------------------------------------------------------------- 循环稳定性（可逆性回归）


def test_load_unload_cycle_returns_to_baseline(tmp_path: Path, monkeypatch):
    """N 轮加载/卸载后各注册表回到基线，无 sys.modules 残留。"""
    pkg = tmp_path / "cycle_probe"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from arclet.alconna import Alconna\n"
        "from muika.plugin.command import on_alconna\n"
        "on_alconna(Alconna('cycle_probe_cmd'))\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    base_commands = len(_commands)
    base_callers = len(_caller_data)
    for _ in range(3):
        assert load_plugin("cycle_probe") is not None
        assert any(c.alc.command == "cycle_probe_cmd" for c in _commands)
        assert unload_plugin("cycle_probe")
        assert all(c.alc.command != "cycle_probe_cmd" for c in _commands)

    assert len(_commands) == base_commands
    assert len(_caller_data) == base_callers
    assert "cycle_probe" not in sys.modules
    assert "cycle_probe" not in _declared_plugins


# --------------------------------------------------------------------------- PluginWatcher path derivation


def test_watcher_derives_package_name_for_file_plugin(tmp_path: Path):
    """PluginFileHandler._derive_package_name 应识别 plugins/<name>.py。"""
    from muika.plugin.watcher import PluginFileHandler

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    fake_file = plugins_dir / "my_plugin.py"
    fake_file.write_text("# placeholder")

    handler = PluginFileHandler(manager=MagicMock(), plugins_dir=plugins_dir, base_path=tmp_path)
    package = handler._derive_package_name(fake_file)
    assert package is not None
    assert package.endswith("my_plugin")


def test_watcher_derives_package_name_for_dir_plugin(tmp_path: Path):
    """PluginFileHandler._derive_package_name 应识别 plugins/<name>/__init__.py。"""
    from muika.plugin.watcher import PluginFileHandler

    plugins_dir = tmp_path / "plugins"
    pkg_dir = plugins_dir / "my_pkg"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("# init")
    inner = pkg_dir / "submodule.py"
    inner.write_text("# sub")

    handler = PluginFileHandler(manager=MagicMock(), plugins_dir=plugins_dir, base_path=tmp_path)
    # 任意子文件都应推导出顶层 package
    package = handler._derive_package_name(inner)
    assert package is not None
    assert "my_pkg" in package


# --------------------------------------------------------------------------- .plugins command module


def test_plugins_plugin_module_structure():
    from muika.builtin_plugins import plugins

    assert hasattr(plugins, "metadata")
    assert plugins.metadata.name == "plugins"
    assert hasattr(plugins, "plugins_cmd")


# --------------------------------------------------------------------------- builtin guard


def test_builtin_prefix_constant_matches_actual_builtins():
    """确保 _BUILTIN_PREFIX 与实际 builtin_plugins 路径一致。"""
    assert _BUILTIN_PREFIX == "muika.builtin_plugins"
