"""验证系统工具在可选 Windows 依赖缺失时仍可加载。"""

import builtins
import runpy
from types import SimpleNamespace

import pytest

from muika.core.actions.tools import _system


@pytest.mark.parametrize("missing_module", ["win32gui", "win32process"])
async def test_missing_pywin32_does_not_break_tool_module_import(monkeypatch, missing_module):
    original_import = builtins.__import__
    attempted = []

    def import_without_pywin32(name, *args, **kwargs):
        if name in {"win32gui", "win32process"}:
            attempted.append(name)
            if name == missing_module:
                raise ImportError(f"Cannot load {name}")
            return SimpleNamespace()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("muika.plugin.func_call.on_function_call", lambda *args, **kwargs: lambda function: function)
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(builtins, "__import__", import_without_pywin32)
    namespace = runpy.run_path(_system.__file__)
    assert missing_module in attempted
    assert callable(namespace["list_processes"])
    result = await namespace["get_focused_window"]()
    assert "pywin32 could not be loaded" in result
