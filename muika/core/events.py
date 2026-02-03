from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal, Optional, TypeAlias

from muika.models import Message

if TYPE_CHECKING:
    from .executor import ActionResult
    from .intents import Intent


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


@dataclass
class ScheduledTriggerPayload:
    when: str
    what: str


@dataclass
class ActionFeedbackPayload:
    intent: "Intent"
    result: Optional["ActionResult"]


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
class RSSUpdateEvent:
    payload: RSSUpdate
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["rss_update"] = "rss_update"


@dataclass(frozen=True)
class InternalReflectionEvent:
    payload: InternalReflection
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["internal_reflection"] = "internal_reflection"


@dataclass(frozen=True)
class ScheduledTriggerEvent:
    payload: ScheduledTriggerPayload
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["scheduled_trigger"] = "scheduled_trigger"


@dataclass(frozen=True)
class ActionFeedbackEvent:
    payload: ActionFeedbackPayload
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["action_feedback"] = "action_feedback"


Event: TypeAlias = (
    UserMessageEvent
    | RSSUpdateEvent
    | TimeTickEvent
    | InternalReflectionEvent
    | ScheduledTriggerEvent
    | ActionFeedbackEvent
)
