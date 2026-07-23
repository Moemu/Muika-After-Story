"""Muika Core -- persona engine.

Provides state, events, and supporting data types at package level.
The ``Muika`` class (event loop) should be imported directly from
``muika.core.loop`` to avoid pulling in heavy dependencies (LLM, DB)
at package import time.
"""

from .events import (
    Event,
    RSSUpdate,
    SessionBootstrapEvent,
    SessionEndEvent,
    TimeTickPayload,
    UserMessagePayload,
)
from .state import MuikaState

__all__ = [
    "Event",
    "MuikaState",
    "RSSUpdate",
    "SessionBootstrapEvent",
    "SessionEndEvent",
    "TimeTickPayload",
    "UserMessagePayload",
]
