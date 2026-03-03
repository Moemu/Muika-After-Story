import asyncio
from datetime import datetime

from nonebot import get_bot
from nonebot_plugin_alconna.uniseg import Target, UniMessage

from muika.config import mas_config

from .scheduler import Scheduler


class Executor:
    def __init__(self, event_queue: asyncio.Queue) -> None:
        self.master_id = mas_config.master_id
        self._cooldown: dict[str, datetime] = {}
        self.scheduler = Scheduler(event_queue=event_queue)

    async def send_message(self, message: str):
        """
        发送消息给用户
        """
        target = Target(self.master_id, private=True)
        await UniMessage(message).send(target=target, bot=get_bot())

    async def _delayed_send(self, content: str, delay: int):
        await asyncio.sleep(delay)
        await self.send_message(content)
