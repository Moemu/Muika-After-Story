"""话题历史、阅读缓存和模型用量的部署业务接口。"""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from muika.node.clock import LocalTimeText


class TopicHistory(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    topic_id: str
    last_used_at: LocalTimeText
    use_count: int
    engaged_count: int


class ReadingCache(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    topic_id: str
    source_id: str
    link: str
    title: str
    published: str | None
    score: int
    keep: int
    reason: str
    primary_theme: str
    summary: str
    fetched_at: LocalTimeText = ""
    evaluated_at: LocalTimeText = ""


class UsageRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    plugin: str
    model: str
    date: str = ""
    type: Literal["chat", "embedding"] = "chat"
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)


class ActivityOperation(BaseModel):
    """只接受具名业务操作，字段含义由各分支显式验证。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal[
        "topic_get", "topic_used", "topics", "reading_get", "reading_save", "reading_prune", "usage_save", "usage_list"
    ]
    topic_id: str = ""
    user_engaged: bool = False
    limit: int = Field(default=10000, ge=1, le=10000)
    days: int | None = Field(default=7, ge=1)
    reading: ReadingCache | None = None
    usage: UsageRecord | None = None


class ActivityResult(BaseModel):
    topics: list[TopicHistory] = Field(default_factory=list)
    reading: ReadingCache | None = None
    usage: list[UsageRecord] = Field(default_factory=list)
    deleted: int = 0


ActivityCall = Callable[[ActivityOperation], Awaitable[ActivityResult]]
_remote_activity: ContextVar[ActivityCall | None] = ContextVar("remote_activity", default=None)


def activity_client() -> ActivityCall | None:
    return _remote_activity.get()


@contextmanager
def route_activity(client: ActivityCall | None) -> Iterator[None]:
    token = _remote_activity.set(client)
    try:
        yield
    finally:
        _remote_activity.reset(token)
