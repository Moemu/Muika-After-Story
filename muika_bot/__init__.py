"""Muika-After-Story Bot -- NoneBot plugin entry point.

This package contains all Bot-process code:
- NoneBot plugin registration and lifecycle management
- Message handlers, debug/model commands
- IPC client (connects to the remote Core process)
- Message merging, user agreement, and other Bot-only utilities
"""

from nonebot import get_driver, require
from nonebot.plugin import PluginMetadata, inherit_supported_adapters

from muika.utils.logger import init_logger, logger

from .config import BotConfig, bot_config

require("nonebot_plugin_alconna")

__plugin_meta__ = PluginMetadata(
    name="Muika-After-Story",
    description="I'll be back to see you.",
    usage="*Pending*",
    type="application",
    config=BotConfig,
    homepage="https://github.com/Moemu/Muika-After-Story",
    extra={},
    supported_adapters=inherit_supported_adapters("nonebot_plugin_alconna"),
)


# Sync master_id from NoneBot superusers if not set via env var
_driver = get_driver()
if not bot_config.master_id and _driver.config.superusers:
    bot_config.master_id = list(_driver.config.superusers)[0]
    logger.debug(f"[Config] Synced master_id from NoneBot superusers: {bot_config.master_id}")

if not bot_config.master_id:
    raise ValueError("Set MASTER_ID to select the player account.")

init_logger()

from . import handlers  # noqa: E402, F401
