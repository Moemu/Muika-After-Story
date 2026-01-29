import importlib
from functools import lru_cache

ADAPTER_CLASSES = {}
"""动态适配器注册表"""


@lru_cache()
def safe_import(path: str):
    """
    安全导入：即使导入出现问题也不会出现报错
    """
    try:
        module_path, class_name = path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except ImportError:
        return None


ADAPTER_CLASSES = {
    "onebot_v12": safe_import("nonebot.adapters.onebot.v12.Bot"),
    "UnsupportedParam": safe_import("nonebot.adapters.onebot.v12.exception.UnsupportedParam"),
    "onebot_v11": safe_import("nonebot.adapters.onebot.v11.Bot"),
    "telegram_event": safe_import("nonebot.adapters.telegram.Event"),
    "telegram_file": safe_import("nonebot.adapters.telegram.message.File"),
    "qq_event": safe_import("nonebot.adapters.telegram.qq.Event"),
}
