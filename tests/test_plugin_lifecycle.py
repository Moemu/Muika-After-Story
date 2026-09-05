"""插件生命周期测试：ownership 追踪、unload/reload、.plugins 命令、PluginWatcher 推导。"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from muika.plugin import lifecycle as lifecycle_mod
from muika.plugin import loader as loader_mod
from muika.plugin import state as state_mod
from muika.plugin.command import (
    CommandDispatcher,
    _commands,
    on_alconna,
    remove_commands_for_plugin,
)
from muika.plugin.ctx import ctx
from muika.plugin.exceptions import (
    PluginConflictError,
    PluginImportError,
    PluginLoadHookError,
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
from muika.plugin.manager import PluginManager


@pytest.fixture(autouse=True)
def _isolate_registries():
    """每个测试前后保存并恢复全部插件注册表、钩子表、状态存储与加载上下文。"""
    saved_commands = list(_commands)
    saved_callers = dict(_caller_data)
    saved_plugins = dict(_plugins)
    saved_declared = set(_declared_plugins)
    saved_load_hooks = {k: list(v) for k, v in lifecycle_mod._load_hooks.items()}
    saved_unload_hooks = {k: list(v) for k, v in lifecycle_mod._unload_hooks.items()}
    saved_store = {k: dict(v) for k, v in state_mod._store.items()}
    saved_loading = _loading_plugin.get()
    yield
    _commands[:] = saved_commands
    _caller_data.clear()
    _caller_data.update(saved_callers)
    _plugins.clear()
    _plugins.update(saved_plugins)
    _declared_plugins.clear()
    _declared_plugins.update(saved_declared)
    lifecycle_mod._load_hooks.clear()
    lifecycle_mod._load_hooks.update(saved_load_hooks)
    lifecycle_mod._unload_hooks.clear()
    lifecycle_mod._unload_hooks.update(saved_unload_hooks)
    state_mod._store.clear()
    state_mod._store.update(saved_store)
    _loading_plugin.set(saved_loading)


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


def test_unload_plugin_removes_all_registrations_and_sys_modules():
    """unload_plugin 应从 _plugins / _declared_plugins / _commands / _caller_data / sys.modules 全部清理。"""
    fake_mod = types.ModuleType("plugins.fake_unload_test")
    fake_mod.__path__ = []  # 标记为 package
    sys.modules["plugins.fake_unload_test"] = fake_mod
    sys.modules["plugins.fake_unload_test.sub"] = types.ModuleType("plugins.fake_unload_test.sub")

    plugin = loader_mod.Plugin(name="fake", module=fake_mod, package_name="plugins.fake_unload_test", meta=None)
    _plugins["plugins.fake_unload_test"] = plugin
    _declared_plugins.add("plugins.fake_unload_test")

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


def test_plugin_manager_refuses_builtin_unload():
    mgr = PluginManager()
    assert mgr.unload("muika.builtin_plugins.reflect") is False
    assert mgr.reload("muika.builtin_plugins.reflect") is False


def test_command_dispatcher_injects_plugin_manager():
    dispatcher = CommandDispatcher(MagicMock(), AsyncMock())

    assert isinstance(dispatcher._injections[PluginManager], PluginManager)


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


def test_ctx_load_decorator_registers_hook_in_order():
    """@ctx.load 按装饰顺序注册，并原样返回被装饰函数。"""

    def first():
        pass

    def second():
        pass

    token = _loading_plugin.set("plugins.ctx_probe")
    try:
        assert ctx.load(first) is first
        assert ctx.load(second) is second
    finally:
        _loading_plugin.reset(token)

    assert lifecycle_mod._load_hooks["plugins.ctx_probe"] == [first, second]


def test_ctx_unload_decorator_registers_hook():
    """@ctx.unload 注册到 unload 钩子表，并原样返回被装饰函数。"""

    def teardown():
        pass

    token = _loading_plugin.set("plugins.ctx_probe")
    try:
        assert ctx.unload(teardown) is teardown
    finally:
        _loading_plugin.reset(token)

    assert lifecycle_mod._unload_hooks["plugins.ctx_probe"] == [teardown]


def test_ctx_decorator_without_loading_context_returns_func():
    """加载上下文缺失时装饰器警告并原样返回，不注册。"""

    def stray():
        pass

    assert _loading_plugin.get() is None
    assert ctx.load(stray) is stray
    assert ctx.unload(stray) is stray
    assert all(stray not in hooks for hooks in lifecycle_mod._load_hooks.values())
    assert all(stray not in hooks for hooks in lifecycle_mod._unload_hooks.values())


def test_ctx_state_is_per_package_and_stable():
    """ctx.state 按包隔离；同包重复访问返回同一对象（跨重载恢复的基础）。"""
    token = _loading_plugin.set("plugins.state_a")
    try:
        s1 = ctx.state
        s1["k"] = 1
        s2 = ctx.state
    finally:
        _loading_plugin.reset(token)

    assert s1 is s2
    assert s1 is state_mod._store["plugins.state_a"]
    assert s2["k"] == 1

    token = _loading_plugin.set("plugins.state_b")
    try:
        sb = ctx.state
    finally:
        _loading_plugin.reset(token)
    assert sb is not s1


def test_ctx_state_outside_loading_context_raises():
    """加载上下文外访问 ctx.state 应抛 RuntimeError（防止静默写丢状态）。"""
    assert _loading_plugin.get() is None
    with pytest.raises(RuntimeError):
        ctx.state


def test_failed_load_does_not_poison_declared_plugins(monkeypatch):
    """加载失败后 _declared_plugins 应回滚，允许修复后重新加载（热重载核心场景）。"""

    def raiser(name):
        raise RuntimeError("boom")

    real_import = importlib.import_module
    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    with pytest.raises(PluginImportError, match="boom"):
        load_plugin("plugins.broken_probe")
    assert "plugins.broken_probe" not in _declared_plugins

    monkeypatch.setattr(loader_mod.importlib, "import_module", real_import)
    sys.modules["plugins.broken_probe"] = types.ModuleType("plugins.broken_probe")
    try:
        assert load_plugin("plugins.broken_probe") is not None
    finally:
        unload_plugin("plugins.broken_probe")


def test_duplicate_load_raises_without_purging_active_plugin():
    """重复加载应抛语义异常，并保留当前活动插件。"""
    module_name = "plugins.conflict_probe"
    sys.modules[module_name] = types.ModuleType(module_name)
    try:
        active = load_plugin(module_name)
        with pytest.raises(PluginConflictError) as caught:
            load_plugin(module_name)
        assert caught.value.module_name == module_name
        assert _plugins[module_name] is active
    finally:
        unload_plugin(module_name)


def test_failed_load_cleans_partial_registrations(monkeypatch):
    """import 半途失败时，已注册的 commands 应一并清理，不留孤儿注册。"""

    def raiser(name):
        from arclet.alconna import Alconna

        on_alconna(Alconna("partial_probe_cmd"))  # 模拟模块执行半途的注册
        raise RuntimeError("boom")

    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    with pytest.raises(PluginImportError, match="boom"):
        load_plugin("plugins.partial_probe")
    assert all(c.alc.command != "partial_probe_cmd" for c in _commands)
    assert "plugins.partial_probe" not in _declared_plugins


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


def _write_plugin(tmp_path: Path, package_name: str, code: str) -> None:
    """在临时目录中写入插件包。"""
    package_path = tmp_path / package_name
    package_path.mkdir()
    (package_path / "__init__.py").write_text(code, encoding="utf-8")


def test_load_plugin_runs_load_hooks_in_registration_order(tmp_path: Path, monkeypatch):
    """load_plugin 应按注册顺序执行 load 钩子。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "hook_order_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.load\n"
        "def first():\n"
        "    state.setdefault('order', []).append('first')\n"
        "@ctx.load\n"
        "def second():\n"
        "    state['order'].append('second')\n",
    )
    try:
        assert load_plugin("hook_order_probe") is not None
        assert state_mod._store["hook_order_probe"]["order"] == ["first", "second"]
    finally:
        unload_plugin("hook_order_probe")


def test_unload_plugin_runs_unload_hooks_lifo(tmp_path: Path, monkeypatch):
    """unload_plugin 应按注册逆序执行 unload 钩子。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "lifo_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "state.setdefault('order', [])\n"
        "@ctx.unload\n"
        "def first():\n"
        "    state['order'].append('first')\n"
        "@ctx.unload\n"
        "def second():\n"
        "    state['order'].append('second')\n",
    )
    assert load_plugin("lifo_probe") is not None
    assert unload_plugin("lifo_probe") is True
    assert state_mod._store["lifo_probe"]["order"] == ["second", "first"]


def test_load_hook_failure_rolls_back_load_but_keeps_state(tmp_path: Path, monkeypatch):
    """load 钩子失败时应回滚插件并保留状态。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "failing_hook_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.load\n"
        "def write_state():\n"
        "    state['written'] = True\n"
        "@ctx.load\n"
        "def fail():\n"
        "    raise RuntimeError('hook failed')\n",
    )

    with pytest.raises(PluginLoadHookError, match="hook failed"):
        load_plugin("failing_hook_probe")
    assert "failing_hook_probe" not in _plugins
    assert "failing_hook_probe" not in _declared_plugins
    assert "failing_hook_probe" not in lifecycle_mod._load_hooks
    assert state_mod._store["failing_hook_probe"]["written"] is True


def test_unload_hook_exception_is_best_effort(tmp_path: Path, monkeypatch):
    """unload 钩子异常不应阻塞其他钩子和卸载。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "best_effort_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.unload\n"
        "def record():\n"
        "    state['recorded'] = True\n"
        "@ctx.unload\n"
        "def fail():\n"
        "    raise RuntimeError('teardown failed')\n",
    )

    assert load_plugin("best_effort_probe") is not None
    assert unload_plugin("best_effort_probe") is True
    assert state_mod._store["best_effort_probe"]["recorded"] is True
    assert "best_effort_probe" not in _plugins


def test_state_survives_reload_same_object(tmp_path: Path, monkeypatch):
    """状态对象应跨多次热重载存活。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "state_survive_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.load\n"
        "def increment():\n"
        "    state['count'] = state.get('count', 0) + 1\n",
    )
    try:
        assert load_plugin("state_survive_probe") is not None
        state = state_mod._store["state_survive_probe"]
        assert reload_plugin("state_survive_probe") is not None
        assert reload_plugin("state_survive_probe") is not None
        assert state_mod._store["state_survive_probe"] is state
        assert state["count"] == 3
    finally:
        unload_plugin("state_survive_probe")


def test_hooks_cleared_on_unload_and_not_duplicated_on_reload(tmp_path: Path, monkeypatch):
    """卸载应清除旧钩子，重载不应重复执行旧钩子。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "no_duplicate_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.load\n"
        "def increment():\n"
        "    state['count'] = state.get('count', 0) + 1\n",
    )
    try:
        assert load_plugin("no_duplicate_probe") is not None
        assert unload_plugin("no_duplicate_probe") is True
        assert "no_duplicate_probe" not in lifecycle_mod._load_hooks
        assert reload_plugin("no_duplicate_probe") is not None
        assert state_mod._store["no_duplicate_probe"]["count"] == 2
    finally:
        unload_plugin("no_duplicate_probe")


def test_failed_load_purges_partial_hooks(monkeypatch):
    """import 失败时应清除已注册的部分钩子。"""

    def fail_import(module_name):
        @ctx.load
        def orphan():
            pass

        raise RuntimeError(module_name)

    monkeypatch.setattr(loader_mod.importlib, "import_module", fail_import)
    with pytest.raises(PluginImportError):
        load_plugin("plugins.orphan_probe")
    assert "plugins.orphan_probe" not in lifecycle_mod._load_hooks


def test_manager_shutdown_all_runs_unload_hooks_including_builtin():
    """shutdown_all 应执行用户插件和 builtin 插件的 unload 钩子。"""
    executed: list[str] = []
    lifecycle_mod.register_unload_hook("plugins.user_probe", lambda: executed.append("user"))
    lifecycle_mod.register_unload_hook("muika.builtin_plugins.fake", lambda: executed.append("builtin"))
    _plugins["plugins.user_probe"] = loader_mod.Plugin(
        name="user_probe",
        module=types.ModuleType("plugins.user_probe"),
        package_name="plugins.user_probe",
        meta=None,
    )
    _plugins["muika.builtin_plugins.fake"] = loader_mod.Plugin(
        name="fake",
        module=types.ModuleType("muika.builtin_plugins.fake"),
        package_name="muika.builtin_plugins.fake",
        meta=None,
    )

    PluginManager().shutdown_all()

    assert set(executed) == {"user", "builtin"}


def test_manager_reload_hook_sequence(tmp_path: Path, monkeypatch):
    """manager reload 应依次执行 unload 和 load 钩子。"""
    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "sequence_probe",
        "from muika.plugin.ctx import ctx\n"
        "state = ctx.state\n"
        "@ctx.load\n"
        "def on_load():\n"
        "    state.setdefault('sequence', []).append('load')\n"
        "@ctx.unload\n"
        "def on_unload():\n"
        "    state['sequence'].append('unload')\n",
    )
    try:
        assert load_plugin("sequence_probe") is not None
        assert PluginManager().reload("sequence_probe") is True
        assert state_mod._store["sequence_probe"]["sequence"] == ["load", "unload", "load"]
    finally:
        unload_plugin("sequence_probe")


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


def test_watcher_unloads_deleted_file_plugin(tmp_path: Path):
    from muika.plugin.watcher import PluginFileHandler

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    deleted = plugins_dir / "gone.py"
    manager = MagicMock()
    manager.is_watcher_suppressed.return_value = False
    handler = PluginFileHandler(manager=manager, plugins_dir=plugins_dir, base_path=tmp_path)

    handler._on_any_event(types.SimpleNamespace(src_path=str(deleted), event_type="deleted"))

    manager.unload.assert_called_once_with("plugins.gone")


def test_watcher_debounces_each_plugin_independently(tmp_path: Path):
    from muika.plugin.watcher import PluginFileHandler

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    first = plugins_dir / "first.py"
    second = plugins_dir / "second.py"
    first.write_text("", encoding="utf-8")
    second.write_text("", encoding="utf-8")
    manager = MagicMock()
    manager.is_watcher_suppressed.return_value = False
    handler = PluginFileHandler(manager=manager, plugins_dir=plugins_dir, base_path=tmp_path)

    handler._on_any_event(types.SimpleNamespace(src_path=str(first), event_type="modified"))
    handler._on_any_event(types.SimpleNamespace(src_path=str(second), event_type="modified"))

    assert manager.reload.call_count == 2


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
    package = handler._derive_package_name(inner)
    assert package is not None
    assert "my_pkg" in package


async def test_next_request_tracks_plugin_load_failure_and_unload(tmp_path, monkeypatch, fake_llm_factory):
    from muika.core.butler.agent import ButlerAgent
    from muika.core.state import MuikaState
    from muika.llm import ModelCompletions

    monkeypatch.syspath_prepend(str(tmp_path))
    _write_plugin(
        tmp_path,
        "visible_probe",
        "from muika.plugin.func_call import on_function_call\n"
        "@on_function_call('probe')\ndef visible_probe():\n    return 'ok'\n",
    )
    fake = fake_llm_factory(response=ModelCompletions(text='<agent_result status="completed">done</agent_result>'))
    agent = ButlerAgent.__new__(ButlerAgent)
    agent.model = fake
    agent._skill_manager = MagicMock()
    agent._skill_manager.render_prompt_section.return_value = ""
    monkeypatch.setattr("muika.core.butler.agent.generate_prompt_from_template", lambda *args: "system")

    async def visible():
        await agent.execute_command("test", MuikaState(), MagicMock())
        return {t["function"]["name"] for t in fake.requests[-1].tools}

    assert "visible_probe" not in await visible()
    load_plugin("visible_probe")
    try:
        assert "visible_probe" in await visible()
        path = tmp_path / "visible_probe" / "__init__.py"
        code = path.read_text(encoding="utf-8")
        path.write_text(code + "raise RuntimeError('broken candidate')\n", encoding="utf-8")
        assert not PluginManager().reload("visible_probe")
        assert "visible_probe" not in await visible()
        path.write_text(code, encoding="utf-8")
        assert PluginManager().reload("visible_probe")
        assert "visible_probe" in await visible()
        assert PluginManager().unload("visible_probe")
        assert "visible_probe" not in await visible()
    finally:
        unload_plugin("visible_probe")
