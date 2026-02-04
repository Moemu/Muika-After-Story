from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from ..intents import SendMessageIntent
from ..state import MuikaState
from ._registry import register_action

if TYPE_CHECKING:
    from ..executor import Executor


@register_action("send_message")
async def handle_send_message(intent: SendMessageIntent, state: MuikaState, executor: "Executor") -> str:
    await executor.send_message(intent.content)

    # 行为反作用
    state.loneliness *= 0.3
    state.attention = min(1.0, state.attention + 0.2)
    state.last_interaction = datetime.now()

    return "Message sent."
