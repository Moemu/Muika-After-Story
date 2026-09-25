"""E2E 测试夹具：剧本化 LLM、进程内 Core 编排器与运行轨迹记录。"""

from .assertions import assert_clean_visible, assert_not_persisted
from .core_app import CoreApp
from .ipc_wire import IpcWire
from .scripted_llm import ScriptedLLM, ScriptedTurn
from .trace import TraceRecorder

__all__ = [
    "CoreApp",
    "IpcWire",
    "ScriptedLLM",
    "ScriptedTurn",
    "TraceRecorder",
    "assert_clean_visible",
    "assert_not_persisted",
]
