from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal, Optional, TypeAlias

from muika.config import mas_config
from muika.models import Message

if TYPE_CHECKING:
    from muika.ipc.server import AdapterInfo


def _get_last_connection_time() -> Optional[datetime]:
    """
    从日志文件中获取上一次对话的时间
    """
    RECORDS_PATH = mas_config.data_dir / "connection_records"
    RECORDS_PATH.mkdir(exist_ok=True, parents=True)

    pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}\.txt$")

    log_files = [p for p in RECORDS_PATH.iterdir() if p.is_file() and pattern.match(p.name)]

    if not log_files:
        return None

    last_file = max(log_files, key=lambda p: p.stem).stem

    return datetime.strptime(last_file, "%Y-%m-%d %H-%M-%S")


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


@dataclass(frozen=True)
class SessionBootstrapEvent:
    timestamp: datetime = field(default_factory=datetime.now)
    last_chat_time: Optional[datetime] = field(default_factory=_get_last_connection_time)
    type: Literal["session_bootstrap"] = "session_bootstrap"

    @property
    def absence_bucket(self) -> str:
        """计算当前 Session 的缺席时间段"""
        if not self.last_chat_time:
            return "short"
        absence_duration = datetime.now() - self.last_chat_time
        if absence_duration < timedelta(hours=3):
            return "short"
        elif absence_duration < timedelta(days=1):
            return "medium"
        else:
            return "long"


@dataclass(frozen=True)
class SessionEndEvent:
    """Session 结束事件——由空闲超时或其他来源触发。
    Loop 收到此事件后调用 Butler 归纳摘要、写入 ARCHIVE，最后重置 Session。
    """

    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["session_end"] = "session_end"


@dataclass(frozen=True)
class AdapterOnlineEvent:
    """新适配器接入事件。"""

    adapter: "AdapterInfo"
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["adapter_online"] = "adapter_online"


@dataclass(frozen=True)
class AdapterOfflineEvent:
    """适配器断开事件。"""

    adapter: "AdapterInfo"
    timestamp: datetime = field(default_factory=datetime.now)
    type: Literal["adapter_offline"] = "adapter_offline"


Event: TypeAlias = (
    UserMessageEvent
    | TimeTickEvent
    | ScheduledTriggerEvent
    | SessionBootstrapEvent
    | SessionEndEvent
    | AdapterOnlineEvent
    | AdapterOfflineEvent
)
