"""插件加载异常。"""

from __future__ import annotations

from typing import Optional


class PluginLoadError(RuntimeError):
    """插件加载失败。"""

    phase = "load"
    default_detail = "unknown error"

    def __init__(self, module_name: str, cause: Optional[BaseException] = None) -> None:
        self.module_name = module_name
        self.cause = cause
        detail = f"{type(cause).__name__}: {cause}" if cause is not None else self.default_detail
        super().__init__(f"Plugin {module_name!r} failed during {self.phase}: {detail}")


class PluginConflictError(PluginLoadError):
    """插件包名已登记。"""

    phase = "conflict check"
    default_detail = "the package name is already registered"


class PluginImportError(PluginLoadError):
    """插件 import 失败。"""

    phase = "import"


class PluginRegistrationError(PluginLoadError):
    """插件对象登记失败。"""

    phase = "registration"


class PluginLoadHookError(PluginLoadError):
    """插件 load 钩子失败。"""

    phase = "load hook"


class PluginReloadError(PluginLoadError):
    """插件卸载后无法开始 reload。"""

    phase = "reload"
    default_detail = "the loaded plugin could not be unloaded"
