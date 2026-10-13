import importlib

import nonebot
from nonebot.adapters import Adapter
from nonebot.drivers import Driver
from pydantic import TypeAdapter


def load_specified_adapter(driver: Driver, adapter: str) -> None:
    """导入并注册指定的 NoneBot 适配器，加载失败时停止启动。"""
    try:
        module = importlib.import_module(adapter)
    except ModuleNotFoundError as error:
        if error.name and (error.name == adapter or adapter.startswith(f"{error.name}.")):
            raise SystemExit(
                f"Adapter module '{adapter}' is not installed. Install its package first. {error}"
            ) from error
        raise SystemExit(f"Adapter '{adapter}' has a missing dependency '{error.name}': {error}") from error
    except ImportError as error:
        raise SystemExit(f"Adapter '{adapter}' failed to import: {error}") from error
    adapter_class = getattr(module, "Adapter", None)
    if not isinstance(adapter_class, type) or not issubclass(adapter_class, Adapter):
        raise SystemExit(f"Module '{adapter}' must export an Adapter subclass. Check ENABLE_ADAPTERS.")
    driver.register_adapter(adapter_class)


nonebot.init()

driver = nonebot.get_driver()

try:
    enable_adapters = TypeAdapter(list[str]).validate_python(
        driver.config.model_dump().get("enable_adapters", ["nonebot.adapters.onebot.v11"]), strict=True
    )
    if not enable_adapters or any(not adapter.strip() for adapter in enable_adapters):
        raise ValueError("Empty adapter list or module name")
except ValueError as error:
    raise SystemExit("ENABLE_ADAPTERS must be a non-empty list of non-empty module names.") from error

for adapter in enable_adapters:
    load_specified_adapter(driver, adapter)

nonebot.load_plugin("muika_bot")

nonebot.run()
