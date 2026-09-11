from __future__ import annotations

import ssl
import time
from typing import Optional

import httpx
from nonebot import get_bot
from nonebot.adapters import Event, MessageSegment

from muika.config import mas_config
from muika.utils.logger import logger

from .adapters import ADAPTER_CLASSES

FILES_DIR = mas_config.data_dir / "downloads"
FILES_DIR.mkdir(parents=True, exist_ok=True)

User_Agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    "AppleWebKit/537.36 (KHTML, like Gecko)"
    "Chrome/134.0.0.0 Safari/537.36 Edg/134.0.0.0"
)


async def download_file(file_url: str, file_name: Optional[str] = None, proxy: Optional[str] = None) -> str:
    """
    保存文件至本地目录(在未提供后缀的情况下, 默认为.jpg后缀)

    :param file_url: 图片在线地址
    :param file_name: 要保存的文件名
    :param proxy: 代理地址

    :return: 保存后的本地目录
    """
    ssl_context = ssl.create_default_context()
    ssl_context.set_ciphers("DEFAULT")
    file_subfix = file_url.split(".")[-1].lower() if "." in file_url else "jpg"
    file_name = file_name if file_name else f"{time.time_ns()}.{file_subfix}"

    async with httpx.AsyncClient(proxy=proxy, verify=ssl_context) as client:
        r = await client.get(file_url, headers={"User-Agent": User_Agent})
        file_dir = FILES_DIR
        local_path = (file_dir / file_name).resolve()
        with open(local_path, "wb") as file:
            file.write(r.content)
        return str(local_path)


async def get_file_via_adapter(message: MessageSegment, event: Event) -> Optional[str]:
    """
    通过适配器自有方式获取文件地址并保存到本地

    :return: 本地地址
    """
    bot = get_bot()

    Onebotv12Bot = ADAPTER_CLASSES["onebot_v12"]
    UnsupportedParam = ADAPTER_CLASSES["UnsupportedParam"]
    Onebotv11Bot = ADAPTER_CLASSES["onebot_v11"]
    TelegramEvent = ADAPTER_CLASSES["telegram_event"]
    TelegramFile = ADAPTER_CLASSES["telegram_file"]

    if Onebotv12Bot and UnsupportedParam and isinstance(bot, Onebotv12Bot):
        # if message.type != "image":
        #     return None

        try:
            file_path = await bot.get_file(type="url", file_id=message.data["file_id"])
        except UnsupportedParam as e:
            logger.error(f"Onebot 实现不支持获取文件 URL，文件获取操作失败：{e}")
            return None

        return str(file_path)

    elif Onebotv11Bot and isinstance(bot, Onebotv11Bot):
        if "url" in message.data and "file" in message.data:
            return await download_file(message.data["url"], message.data["file"])

    elif TelegramEvent and TelegramFile and isinstance(event, TelegramEvent):
        if not isinstance(message, TelegramFile):
            return None

        file_id = message.data["file"]
        file = await bot.get_file(file_id=file_id)
        if not file.file_path:
            return None

        url = f"https://api.telegram.org/file/bot{bot.bot_config.token}/{file.file_path}"  # type: ignore
        # filename = file.file_path.split("/")[1]
        return await download_file(url, proxy=mas_config.telegram_proxy)

    return None
