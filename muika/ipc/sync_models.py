"""描述本地已经产生的记忆与行动结果，不定义请求和回复配对。"""

from datetime import date, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from muika.core.agent.task_store import CallRecord, TaskRecord
from muika.core.memory_models import Diary, Experience, Fact, MemorySnapshot
from muika.core.scheduler import Reminder
from muika.core.state import StateRhythm

SYNC_PROTOCOL = "1"
MAX_SYNC_BYTES = 32 * 1024 * 1024


class Attachment(BaseModel):
    """临时或二进制附件保存后的引用，不传输内存流对象。"""

    type: Literal["image", "video", "audio", "file"]
    path: str = ""
    url: str | None = None
    mimetype: str | None = None


class RecordedExperience(Experience):
    """附带已保存附件的经历；编号属于来源节点。"""

    resources: list[Attachment] = Field(default_factory=list)


class RecordedFact(Fact):
    """保存事实版本及遗忘结果，避免接收方重新推导。"""

    active: bool = True
    forgotten: bool = False
    created_at: datetime


class RecordedRecall(BaseModel):
    """一个日记日内已完成的事实强化。"""

    fact_id: int
    day: date


class Activity(BaseModel):
    """一次本地提交产生的领域结果；允许完全没有可见输出。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex)
    origin: str
    timestamp: datetime = Field(default_factory=datetime.now)
    snapshot: MemorySnapshot | None = None
    rhythm: StateRhythm | None = None
    references: dict[str, str] = Field(default_factory=dict)
    """领域引用到其原始来源的映射；数据库主键不在节点间共用。"""
    experiences: list[RecordedExperience] = Field(default_factory=list)
    facts: list[RecordedFact] = Field(default_factory=list)
    diaries: list[Diary] = Field(default_factory=list)
    tasks: list[TaskRecord] = Field(default_factory=list)
    calls: list[CallRecord] = Field(default_factory=list)
    recalls: list[RecordedRecall] = Field(default_factory=list)
    reminders: list[Reminder] = Field(default_factory=list)


class SyncEntry(BaseModel):
    """本地或 Gateway 历史中的位置；不表示消息投递状态。"""

    sequence: int = 0
    gateway_sequence: int | None = None
    activity: Activity
