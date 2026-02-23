from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ...state import MuikaState
from ..intents import SendMessageIntent
from ..registry import register_intent

if TYPE_CHECKING:
    from ...executor import Executor


@register_intent("send_message")
async def handle_send_message(intent: SendMessageIntent, state: MuikaState, executor: "Executor") -> str:
    await executor.send_message(intent.content)

    state.loneliness *= 0.3
    state.attention = min(1.0, state.attention + 0.2)
    state.last_interaction = datetime.now()

    return "Message sent."
