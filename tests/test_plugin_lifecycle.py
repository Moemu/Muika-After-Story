"""插件生命周期测试：ownership 追踪、unload/reload、.plugins 命令、PluginWatcher 推导。"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from muika.core.memory_reasoning import MemoryReasoner
from muika.plugin import lifecycle as lifecycle_mod
from muika.plugin import loader as loader_mod
from muika.plugin import state as state_mod
from muika.plugin.command import (
    _commands,
    get_commands,
    on_alconna,
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
    get_function_calls,
    on_function_call,
)
from muika.plugin.loader import (
    _declared_plugins,
    _loading_plugin,
    _plugins,
    get_plugins,
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


def test_unload_nonexistent_returns_false():
    assert unload_plugin("plugins.does.not.exist") is False


def test_plugin_manager_refuses_builtin_unload():
    mgr = PluginManager()
    assert mgr.unload("muika.builtin_plugins.reflect") is False
    assert mgr.reload("muika.builtin_plugins.reflect") is False


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


def test_ctx_decorators_return_the_decorated_function():
    """@ctx.load / @ctx.unload 注册钩子后原样返回被装饰函数；顺序与 LIFO 由真实插件 keeper 证明。"""

    def on_load():
        pass

    def on_unload():
        pass

    token = _loading_plugin.set("plugins.ctx_probe")
    try:
        assert ctx.load(on_load) is on_load
        assert ctx.unload(on_unload) is on_unload
    finally:
        _loading_plugin.reset(token)


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


def test_failed_load_can_be_retried_after_fix(monkeypatch):
    """加载失败后修复模块即可重试成功，上一次失败不残留登记（热重载核心场景）。"""

    def raiser(name):
        raise RuntimeError("boom")

    real_import = importlib.import_module
    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    with pytest.raises(PluginImportError, match="boom"):
        load_plugin("plugins.broken_probe")

    # 重试成功即证明声明登记已回滚：否则会抛 PluginConflictError
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


def test_failed_load_purges_partial_registrations_and_hooks(monkeypatch):
    """import 半途失败时，已注册的 commands 与钩子应一并清理，不留孤儿注册。"""

    def raiser(name):
        from arclet.alconna import Alconna

        on_alconna(Alconna("partial_probe_cmd"))  # 模拟模块执行半途的注册
        ctx.load(lambda: None)
        raise RuntimeError("boom")

    monkeypatch.setattr(loader_mod.importlib, "import_module", raiser)
    with pytest.raises(PluginImportError, match="boom"):
        load_plugin("plugins.partial_probe")
    assert "partial_probe_cmd" not in {item.alc.command for item in get_commands()}
    assert "plugins.partial_probe" not in lifecycle_mod._load_hooks


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


def core_probe_tool():
    """代表 core 在插件加载上下文之外注册的工具（无插件归属）。"""
    return "core"


def test_load_unload_cycle_returns_to_baseline(tmp_path: Path, monkeypatch):
    """N 轮加载/卸载后命令/工具/插件登记/sys.modules 回基线；卸载 A 不影响 B 与 core。"""
    cycle = tmp_path / "cycle_probe"
    cycle.mkdir()
    (cycle / "sub.py").write_text("VALUE = 1\n", encoding="utf-8")
    (cycle / "__init__.py").write_text(
        "from arclet.alconna import Alconna\n"
        "from muika.plugin.command import on_alconna\n"
        "from muika.plugin.func_call import on_function_call\n"
        "from . import sub\n"
        "on_alconna(Alconna('cycle_a_cmd'))\n"
        "@on_function_call('cycle a tool')\n"
        "def cycle_a_tool():\n"
        "    return sub.VALUE\n",
        encoding="utf-8",
    )
    other = tmp_path / "other_plugin"
    other.mkdir()
    (other / "__init__.py").write_text(
        "from arclet.alconna import Alconna\n"
        "from muika.plugin.command import on_alconna\n"
        "from muika.plugin.func_call import on_function_call\n"
        "on_alconna(Alconna('cycle_b_cmd'))\n"
        "@on_function_call('cycle b tool')\n"
        "def cycle_b_tool():\n"
        "    return 'b'\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    from arclet.alconna import Alconna

    # core 在加载上下文之外的注册（无插件归属）：任何用户插件卸载都不得清除它们
    on_alconna(Alconna("core_probe_cmd"))
    on_function_call(description="core probe tool")(core_probe_tool)

    def commands() -> set[str]:
        return {item.alc.command for item in get_commands()}

    def tools() -> set[str]:
        return set(get_function_calls())

    base_commands, base_tools, base_plugins = commands(), tools(), set(get_plugins())
    assert "core_probe_cmd" in base_commands
    assert "core_probe_tool" in base_tools

    for _ in range(3):
        assert load_plugin("cycle_probe") is not None
        assert load_plugin("other_plugin") is not None
        assert {"cycle_a_cmd", "cycle_b_cmd"} <= commands()
        assert {"cycle_a_tool", "cycle_b_tool"} <= tools()
        assert "cycle_probe.sub" in sys.modules  # 插件包内的子模块确实被导入

        assert unload_plugin("cycle_probe")
        assert "cycle_a_cmd" not in commands()  # 只清 A 的命令
        assert "cycle_b_cmd" in commands()  # B 的命令仍在
        assert "core_probe_cmd" in commands()  # core 命令仍在
        assert "cycle_a_tool" not in tools()
        assert "cycle_b_tool" in tools()  # 只清 A 的工具
        assert "core_probe_tool" in tools()

        assert unload_plugin("other_plugin")
        assert commands() == base_commands
        assert tools() == base_tools
        assert set(get_plugins()) == base_plugins
        assert "cycle_probe" not in sys.modules
        assert "cycle_probe.sub" not in sys.modules
        assert "other_plugin" not in sys.modules


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


def test_watcher_unloads_deleted_file_plugin(tmp_path: Path):
    from muika.plugin.watcher import PluginFileHandler

    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    deleted = plugins_dir / "gone.py"
    manager = MagicMock()
    manager.is_watcher_suppressed.return_value = False
    handler = PluginFileHandler(manager=manager, plugins_dir=plugins_dir, base_path=tmp_path)

    handler._on_any_event(types.SimpleNamespace(src_path=str(deleted), event_type="deleted"))

    manager.unload.assert_called_once_with("plugins.gone", origin="runtime")


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


@pytest.mark.parametrize(
    "relative",
    [
        "README.md",
        "LICENSE",
        "package.json",
        "package-lock.json",
        ".github/workflows/build.yaml",
        "node_modules/tailwindcss/index.js",
        "build.config.js",
        "mas_plugin_daily/README.md",
        "mas_plugin_daily/node_modules/tool/index.js",
    ],
)
def test_watcher_ignores_docs_dependencies_and_build_files(tmp_path, relative):
    from muika.plugin.watcher import PluginFileHandler

    plugins = tmp_path / "plugins"
    root = plugins / "MAS-Plugin-Daily"
    package = root / "mas_plugin_daily"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    source = root / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("")
    manager = MagicMock()
    manager.is_watcher_suppressed.return_value = False
    handler = PluginFileHandler(manager, plugins, tmp_path)
    handler._handle_path(source, is_delete=False)
    source.unlink()
    handler._handle_path(source, is_delete=True)
    manager.reload.assert_not_called()
    manager.unload.assert_not_called()


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("MAS-Plugin-Daily/mas_plugin_daily/__init__.py", "plugins.MAS-Plugin-Daily.mas_plugin_daily"),
        ("MAS-Plugin-Daily/mas_plugin_daily/info.py", "plugins.MAS-Plugin-Daily.mas_plugin_daily"),
        ("MAS-Plugin-Daily/mas_plugin_daily/template/index.html.jinja", "plugins.MAS-Plugin-Daily.mas_plugin_daily"),
        ("MAS-Plugin-Daily/mas_plugin_daily/static/css/output.css", "plugins.MAS-Plugin-Daily.mas_plugin_daily"),
        ("MAS-Plugin-Daily/mas_plugin_daily/prompts/context.md", "plugins.MAS-Plugin-Daily.mas_plugin_daily"),
        ("my_plugin.py", "plugins.my_plugin"),
        ("my_pkg/submodule.py", "plugins.my_pkg"),
    ],
)
def test_watcher_keeps_runtime_code_and_resource_changes(tmp_path, relative, expected):
    from muika.plugin.watcher import PluginFileHandler

    plugins = tmp_path / "plugins"
    (plugins / "MAS-Plugin-Daily" / "mas_plugin_daily").mkdir(parents=True)
    (plugins / "MAS-Plugin-Daily" / "mas_plugin_daily" / "__init__.py").write_text("")
    (plugins / "my_pkg").mkdir()
    (plugins / "my_pkg" / "__init__.py").write_text("")
    source = plugins / relative
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("")
    manager = MagicMock()
    manager.is_watcher_suppressed.return_value = False
    handler = PluginFileHandler(manager, plugins, tmp_path)
    handler._handle_path(source.parent, is_delete=False)
    manager.reload.assert_not_called()
    handler._handle_path(source, is_delete=False)
    manager.reload.assert_called_once_with(expected, origin="runtime")


async def test_next_request_tracks_plugin_load_failure_and_unload(tmp_path, monkeypatch, fake_llm_factory):
    from muika.core.agent.agent import Agent
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
    agent = Agent.__new__(Agent)
    agent.action_lock = asyncio.Lock()
    agent.model = fake
    agent.summarize_model = fake
    agent.memory_reasoner = MemoryReasoner(fake)
    monkeypatch.setattr("muika.core.agent.agent.get_model_config", lambda name: fake.config)
    agent._skill_manager = MagicMock()
    agent._skill_manager.render_prompt_section.return_value = ""
    monkeypatch.setattr("muika.core.agent.agent.generate_prompt_from_template", lambda *args: "system")

    async def visible():
        request = agent.build_request("test", MuikaState())
        return {t.name for t in request.tools}

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
