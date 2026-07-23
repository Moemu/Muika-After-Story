"""Message executor -- splits and sends text via a pluggable callback."""

import asyncio
from typing import Callable, Coroutine

from .scheduler import Scheduler

COMMON_PUNCTUATION = "。！？；…\n"
DELAYED_SECOND_PER_PARAGRAPH = 1.5

SendFunc = Callable[[str], Coroutine[None, None, None]]
"""Async callback that delivers a single text message to the platform."""


class Executor:
    """Splits long messages into segments and sends them via ``send_func``.

    :param event_queue: shared event queue for the Scheduler.
    :param send_func: async callable that actually delivers a message string.
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
        """Split *content* into paragraphs, breaking long paragraphs on punctuation."""
        messages_split_by_newlines = content.split("\n\n")
        final_messages = []
        for msg in messages_split_by_newlines:
            if len(msg) <= max_length_per_message:
                final_messages.append(msg)
                continue
            messages_split_by_punctuation = []
            current_segment = ""
            for char in msg:
                current_segment += char
                if char in COMMON_PUNCTUATION:
                    messages_split_by_punctuation.append(current_segment)
                    current_segment = ""
            if current_segment:
                messages_split_by_punctuation.append(current_segment)
            final_messages.extend(messages_split_by_punctuation)
        return final_messages

    async def send_message(self, message: str) -> None:
        """Clean up *message*, split it, and deliver each segment via ``send_func``."""
        message = message.strip().replace("\n\n\n\n", "\n\n")
        messages = self._split_message(message)
        for msg in messages:
            await self._send_func(msg)
            await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)

    async def _delayed_send(self, content: str, delay: int) -> None:
        """Send *content* after *delay* seconds."""
        await asyncio.sleep(delay)
        await self.send_message(content)
