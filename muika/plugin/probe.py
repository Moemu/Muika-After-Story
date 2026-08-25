"""在独立进程中验证插件完整生命周期。"""

from __future__ import annotations

import argparse
import importlib
import json
import os

from muika.plugin.loader import try_load_plugin, unload_plugin

_RESULT_PREFIX = "MUIKA_PLUGIN_PROBE="


def probe(module_name: str) -> tuple[bool, str]:
    """加载并卸载一个插件。"""
    plugin_root = os.environ.get("MUIKA_PLUGIN_ROOT")
    if plugin_root:
        package_name = module_name.split(".", 1)[0]
        package = importlib.import_module(package_name)
        package_paths = getattr(package, "__path__", None)
        if package_paths is not None and plugin_root not in package_paths:
            package_paths.append(plugin_root)
    result = try_load_plugin(module_name)
    if not result.success:
        return False, result.error or "Unknown load error"
    if not unload_plugin(module_name):
        return False, "Plugin unload failed"
    return True, "Plugin load and unload succeeded"


def main() -> int:
    """运行命令行探针。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("module_name")
    args = parser.parse_args()
    success, message = probe(args.module_name)
    print(_RESULT_PREFIX + json.dumps({"success": success, "message": message}, ensure_ascii=False))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
