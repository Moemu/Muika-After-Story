from nonebot import require

require("nonebot_plugin_alconna")
require("nonebot_plugin_localstore")
require("nonebot_plugin_apscheduler")
require("nonebot_plugin_orm")

from nonebot.plugin import PluginMetadata, inherit_supported_adapters  # noqa: E402

from .config import PluginConfig  # noqa: E402
from .utils.utils import init_logger  # noqa: E402

init_logger()

from . import database  # noqa: E402, F401

__plugin_meta__ = PluginMetadata(
    name="Muika-After-Story",
    description="I'll be back to see you.",
    usage="*Pending*",
    type="application",
    config=PluginConfig,
    homepage="https://github.com/Moemu/Muika-After-Story",
    extra={},
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
)
