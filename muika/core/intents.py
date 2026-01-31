from typing import Annotated, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, Field


class SendMessageIntent(BaseModel):
    name: Literal["send_message"] = "send_message"
    confidence: float
    reason: Optional[str] = None
    content: str


class DoNothingIntent(BaseModel):
    name: Literal["do_nothing"] = "do_nothing"
    confidence: float
    reason: Optional[str] = None


class CheckRSSUpdateIntent(BaseModel):
    name: Literal["check_rss_update"] = "check_rss_update"
    confidence: float
    reason: Optional[str] = None
    rss_source: str


class PlanFutureEventIntent(BaseModel):
    name: Literal["plan_future_event"] = "plan_future_event"
    confidence: float
    reason: Optional[str] = None
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


Intent: TypeAlias = Annotated[
    Union[
        SendMessageIntent,
        DoNothingIntent,
        CheckRSSUpdateIntent,
        PlanFutureEventIntent,
    ],
    Field(discriminator="name"),
]
