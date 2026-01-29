from enum import Enum, auto
from typing import Awaitable, Callable, Union

from muika.llm import ModelCompletions, ModelRequest, ModelStreamCompletions
from muika.models import Message

SYNC_HOOK_FUNC = Callable[..., None]
ASYNC_HOOK_FUNC = Callable[..., Awaitable[None]]
HOOK_FUNC = Union[SYNC_HOOK_FUNC, ASYNC_HOOK_FUNC]

HOOK_ARGS = Union[Message, ModelCompletions, ModelStreamCompletions, ModelRequest]


class HookType(Enum):
    """可用的 Hook 类型"""

    BEFORE_PRETREATMENT = auto()
    """预处理前"""
    BEFORE_MODEL_COMPLETION = auto()
    """模型调用前"""
    ON_STREAM_CHUNK = auto()
    """模型流式输出中"""
    AFTER_MODEL_COMPLETION = auto()
    """模型调用后"""
    ON_FINISHING_CHAT = auto()
    """结束对话时(存库前)"""
