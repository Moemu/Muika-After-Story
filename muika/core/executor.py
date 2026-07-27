"""Message executor -- splits and sends text via a pluggable callback."""

import asyncio
from typing import Any, Callable, Coroutine, Optional

from .scheduler import Scheduler

COMMON_PUNCTUATION = "。！？；…\n"
DELAYED_SECOND_PER_PARAGRAPH = 1.5

SendFunc = Callable[[str, Optional[list[dict[str, Any]]]], Coroutine[None, None, None]]
"""Async callback that delivers a text message with optional multimodal resources to the platform."""


class Executor:
    """Splits long messages into segments and sends them via ``send_func``.

    :param event_queue: shared event queue for the Scheduler.
    :param send_func: async callable that actually delivers a message string
                      with optional resources.
    """

    def __init__(
        self,
        event_queue: asyncio.Queue,
        send_func: SendFunc,
    ) -> None:
        self.scheduler = Scheduler(event_queue=event_queue)
        self._send_func = send_func

    @staticmethod
    def _split_message(content: str, max_length_per_message: int = 250) -> list[str]:
        """将消息按自然边界切分，贪心合并以最小化切出的消息段数量。"""
        paragraphs = content.split("\n\n")
        final_messages = []

        for paragraph in paragraphs:
            if len(paragraph) <= max_length_per_message:
                final_messages.append(paragraph)
                continue

            # 先按标点切分为自然句段
            segments = []
            current = ""
            for char in paragraph:
                current += char
                if char in COMMON_PUNCTUATION:
                    segments.append(current)
                    current = ""
            if current:
                segments.append(current)

            # 贪心合并句段，使每条消息尽可能接近 max_length_per_message
            buffer = ""
            for seg in segments:
                if len(buffer) + len(seg) <= max_length_per_message:
                    buffer += seg
                else:
                    if buffer:
                        final_messages.append(buffer)
                        buffer = ""
                    # 若单个句段超过上限，硬切分
                    while len(seg) > max_length_per_message:
                        final_messages.append(seg[:max_length_per_message])
                        seg = seg[max_length_per_message:]
                    buffer = seg
            if buffer:
                final_messages.append(buffer)

        return final_messages

    async def send_message(self, message: str, resources: Optional[list[dict[str, Any]]] = None) -> None:
        """Clean up *message*, split it, and deliver each segment via ``send_func``.

        若提供 *resources*，它们将附加到最后一条消息段中。
        """
        message = message.strip().replace("\n\n\n\n", "\n\n")
        messages = self._split_message(message) if message else [""]
        last_idx = len(messages) - 1
        for i, msg in enumerate(messages):
            # 仅最后一段携带 resources
            res = resources if i == last_idx else None
            await self._send_func(msg, res)
            await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)

    async def _delayed_send(self, content: str, delay: int) -> None:
        """Send *content* after *delay* seconds."""
        await asyncio.sleep(delay)
        await self.send_message(content)
