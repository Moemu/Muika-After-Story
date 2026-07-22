import asyncio
from datetime import datetime

from nonebot import get_bot
from nonebot_plugin_alconna.uniseg import Target, UniMessage

from muika.config import mas_config

from .scheduler import Scheduler

COMMON_PUNCTUATION = "。！？；…\n"
DELAYED_SECOND_PER_PARAGRAPH = 1.5


class Executor:
    def __init__(self, event_queue: asyncio.Queue) -> None:
        self.master_id = mas_config.master_id
        self._cooldown: dict[str, datetime] = {}
        self.scheduler = Scheduler(event_queue=event_queue)

    def _split_message(self, content: str, max_length_per_message: int = 250) -> list[str]:
        messages_split_by_newlines = content.split("\n\n")
        final_messages = []
        for msg in messages_split_by_newlines:
            if len(msg) <= max_length_per_message:
                final_messages.append(msg)
                continue
            messages_spilt_by_punctuation = []
            current_segment = ""
            for char in msg:
                current_segment += char
                if char in COMMON_PUNCTUATION:
                    messages_spilt_by_punctuation.append(current_segment)
                    current_segment = ""
            if current_segment:
                messages_spilt_by_punctuation.append(current_segment)

            final_messages.extend(messages_spilt_by_punctuation)
        return final_messages

    async def send_message(self, message: str):
        """
        发送消息给用户
        """
        target = Target(self.master_id, private=True)
        messages = self._split_message(message)
        for msg in messages:
            await UniMessage(msg).send(target=target, bot=get_bot())
            await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)

    async def _delayed_send(self, content: str, delay: int):
        await asyncio.sleep(delay)
        await self.send_message(content)
