"""描述可以持久重投的核心事件，不复制空闲时间步进。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .clock import LocalTime


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal[
        "time_tick",
        "scheduled_trigger",
        "session_end",
        "agent_task",
        "agent_handoff",
        "timeout",
        "device_online",
        "device_offline",
        "device_capability_changed",
        "core_handoff_result",
    ]
    timestamp: LocalTime
    think_mode: Literal["emotional", "topic"] | None = None
    task_id: str = ""
    revision: int = 0
    status: str = ""
    report: str = ""
    when: str = ""
    what: str = ""
    set_at: LocalTime | None = None
    duration: float = 0
