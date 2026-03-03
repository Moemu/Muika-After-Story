from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional, TypeAlias

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
class ScheduledTriggerPayload:
    when: str
    what: str


@dataclass(frozen=True)
class UserMessageEvent:
    payload: UserMessagePayload
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["user_message"] = "user_message"


@dataclass(frozen=True)
class TimeTickEvent:
    payload: TimeTickPayload = field(default_factory=TimeTickPayload)
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["time_tick"] = "time_tick"


@dataclass(frozen=True)
class ScheduledTriggerEvent:
    payload: ScheduledTriggerPayload
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["scheduled_trigger"] = "scheduled_trigger"


Event: TypeAlias = UserMessageEvent | TimeTickEvent | ScheduledTriggerEvent
