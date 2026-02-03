from enum import Enum
from typing import Annotated, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, Field
from pydantic.json_schema import SkipJsonSchema


class Persistence(str, Enum):
    """
    定义意图的生命周期模式
    """

    EPHEMERAL = "ephemeral"
    """
    [转瞬即逝] 现在的想法。
    只在当前 Loop 尝试执行一次。如果被阻塞或失败，直接丢弃。
    """
    SHORT_TERM = "short_term"
    """
    [短期关注] 稍纵即逝的执念。
    保留一段时间。如果在时间窗口内没机会执行，就忘了。
    """
    STICKY = "sticky"
    """
    [长期执念] 不达目的不罢休。
    一直保留在 State 中，直到 Executor 明确返回 Success，或者被逻辑强制取消。
    """


class IntentBase(BaseModel):
    confidence: float
    reason: Optional[str] = None
    persistence: SkipJsonSchema[Persistence] = Field(default=Persistence.EPHEMERAL, exclude=True)
    missed_cycles: SkipJsonSchema[int] = Field(default=0, exclude=True)
    failure_count: SkipJsonSchema[int] = Field(default=0, exclude=True)


class SendMessageIntent(IntentBase):
    name: Literal["send_message"] = "send_message"
    content: str
    persistence: SkipJsonSchema[Persistence] = Field(default=Persistence.SHORT_TERM, exclude=True)


class DoNothingIntent(IntentBase):
    name: Literal["do_nothing"] = "do_nothing"


class CheckRSSUpdateIntent(IntentBase):
    name: Literal["check_rss_update"] = "check_rss_update"
    rss_source: str
    persistence: SkipJsonSchema[Persistence] = Persistence.SHORT_TERM


class PlanFutureEventIntent(IntentBase):
    name: Literal["plan_future_event"] = "plan_future_event"
    when: str = Field(
        ...,
        description="Natural language time description, e.g., 'in 10 minutes', 'tomorrow at 8am', 'tonight'.",
    )
    what: str = Field(
        ...,
        description=(
            "The content or topic to bring up later. " "E.g., 'Remind him to drink water', 'Ask how the meeting went'."
        ),
    )
    persistence: SkipJsonSchema[Persistence] = Persistence.STICKY


Intent: TypeAlias = Annotated[
    Union[
        SendMessageIntent,
        DoNothingIntent,
        CheckRSSUpdateIntent,
        PlanFutureEventIntent,
    ],
    Field(discriminator="name"),
]
