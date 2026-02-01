from nonebot import logger, require
from nonebot.plugin import PluginMetadata, inherit_supported_adapters

require("nonebot_plugin_alconna")
require("nonebot_plugin_localstore")
require("nonebot_plugin_orm")

from .config import MASConfig  # noqa: E402

__plugin_meta__ = PluginMetadata(
    name="Muika-After-Story",
    description="I'll be back to see you.",
    usage="*Pending*",
    type="application",
    config=MASConfig,
    homepage="https://github.com/Moemu/Muika-After-Story",
    extra={},
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
)

import nonebot_plugin_localstore as store  # noqa: E402

from .utils.first_run import user_agreement  # noqa: E402
from .utils.utils import get_version, init_logger  # noqa: E402

init_logger()

logger.info(f"Muika-After-Story 版本: {get_version()}")
logger.info(f"Muika-After-Story 数据目录: {store.get_plugin_data_dir().resolve()}")
user_agreement.check_first_run()

from . import bot  # noqa: E402, F401
from . import database  # noqa: E402, F401
