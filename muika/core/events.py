from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Optional

from muika.models import Message


@dataclass
class UserMessagePayload:
    message: Message


@dataclass
class TimeTickPayload:
    current_time: datetime = field(default_factory=datetime.now)


@dataclass
class RSSUpdate:
    feed: str
    title: str
    content: Optional[str] = None


@dataclass
class InternalReflection:
    internal_monologue: str
    """内在独白"""


@dataclass(frozen=True)
class Event:
    type: Literal[
        "user_message",
        "rss_update",
        "time_tick",
        "internal_reflection",
    ]
    payload: Any
    timestamp: datetime = field(default_factory=datetime.now)
