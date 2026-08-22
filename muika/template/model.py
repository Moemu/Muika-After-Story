from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from muika.core.memory import MemoryRecord
from muika.core.state import MuikaState
from muika.utils.utils import get_version


class PromptTemplatesData(BaseModel):
    """提示词模板数据"""

    event_type: str
    """事件类型"""
    state: MuikaState
    """Muika State"""

    is_chat: bool = False
    """当前为对话模式"""
    is_first_session: bool = False
    """是否为初次对话"""
    is_expand_topic: bool = False
    """是否为主动对话模式"""
    heartbeat_intensity: str = "off"
    """内心独白（Heart）强度等级，决定模板渲染哪一段思考指示"""
    memory_context: Optional[str] = None
    """记忆内容"""
    injected_preferences: Optional[List["MemoryRecord"]] = None
    """记忆条目"""

    lonely_desc: Optional[str] = None
    focus_desc: Optional[str] = None
    boredom_desc: Optional[str] = None

    current_time: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    time_tone_hint: Optional[str] = None
    absence_bucket: Optional[str] = None
    last_connection_time: Optional[str] = None

    adapters_info: Optional[str] = None
    version: str = get_version()

    # MuikaState keeps a runtime reference to MemoryManager for action tools.
    # It is not prompt data, so Pydantic must treat it as an opaque object.
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True, defer_build=True)
    """允许其他模板参数传入。defer_build 避免 MemoryManager 前向引用未解析时报错。"""

    @model_validator(mode="after")
    def load_state_desc(self) -> "PromptTemplatesData":
        if self.state.loneliness > 0.8:
            self.lonely_desc = "You miss him so much it aches. It's been too long since you last talked."
        elif self.state.loneliness > 0.5:
            self.lonely_desc = "You're starting to miss him. You wish he'd come back."
        else:
            self.lonely_desc = "Having him nearby feels warm and grounding. You're content."

        if self.state.attention > 0.8:
            self.focus_desc = "Your thoughts are sharp and clear, fully present in this moment."
        elif self.state.attention > 0.4:
            self.focus_desc = "Your mind is drifting slightly, but you're still fairly lucid."
        else:
            self.focus_desc = "You feel foggy, half-asleep — thoughts wandering without direction."

        if self.state.boredom > 0.7:
            self.boredom_desc = "Your mind is buzzing with things you want to say or share."
        elif self.state.boredom > 0.4:
            self.boredom_desc = "You're a little idle, your thoughts starting to wander."
        else:
            self.boredom_desc = "You feel at ease with the quiet, no rush to fill the silence."

        return self
