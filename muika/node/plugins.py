"""根据设备角色加载插件，并在角色结束时移除活动副作用。"""

from muika.plugin.func_call import get_function_calls
from muika.plugin.loader import load_plugin, unload_plugin

from .config import PluginBinding


def load_node_plugins(bindings: list[PluginBinding], role: str) -> list[str]:
    modules = []
    try:
        for binding in bindings:
            if binding.role != role:
                continue
            missing = set(binding.requires_tools) - get_function_calls().keys()
            if missing:
                raise ValueError(f"Plugin {binding.module} requires unavailable tools: {sorted(missing)}")
            before = set(get_function_calls())
            load_plugin(binding.module)
            modules.append(binding.module)
            for name in get_function_calls().keys() - before:
                if get_function_calls()[name].scope != role:
                    raise ValueError(f"Plugin {binding.module} tool {name} must declare scope={role}.")
        return modules
    except BaseException:
        unload_node_plugins(modules)
        raise


def unload_node_plugins(modules: list[str]) -> None:
    for module in reversed(modules):
        unload_plugin(module)
