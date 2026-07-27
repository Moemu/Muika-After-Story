""".debug —— 调试与情绪状态管理命令。"""

import dataclasses

from arclet.alconna import Alconna, Args, Subcommand

from muika.core.loop import Muika
from muika.core.state import MuikaState
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="debug",
    description="Muika 调试命令",
    usage=".debug <topic|state|state-set|topic-reset>",
)

# 从 MuikaState 动态推导可修改的字段，避免新增字段后需要手动同步
_FLOAT_FIELDS: set[str] = set()
_STR_FIELDS: set[str] = set()
# 内部/引用字段不应通过 state-set 直接修改
_EXCLUDED_FIELDS: set[str] = {"last_interaction", "last_proactive_at", "active_topic", "memory"}

for _f in dataclasses.fields(MuikaState):
    if _f.name.startswith("_") or _f.name in _EXCLUDED_FIELDS:
        continue
    # PEP 563 (from __future__ import annotations) 下 f.type 为字符串
    if _f.type == "float":
        _FLOAT_FIELDS.add(_f.name)
    elif _f.type == "str":
        _STR_FIELDS.add(_f.name)

_ALL_FIELDS = _FLOAT_FIELDS | _STR_FIELDS

alc = Alconna(
    "debug",
    Subcommand("topic", help_text="立即触发一次话题旁路管线", dest="topic"),
    Subcommand("state", help_text="显示当前 Muika 情绪状态", dest="state"),
    Subcommand(
        "state-set",
        Args["field", str]["value", str],
        help_text="修改情绪状态字段",
        dest="set",
    ),
    Subcommand("topic-reset", help_text="清空当前活跃话题", dest="reset"),
)
debug_cmd = on_alconna(alc)


@debug_cmd.assign("topic")
async def _trigger_topic(muika: Muika) -> str:
    await muika._run_topic_pipeline()
    return "[System] 已触发话题管线"


@debug_cmd.assign("state")
async def _show_state(state: MuikaState) -> str:
    at = state.active_topic
    lines = [
        "当前 Muika 情绪状态:",
        f"  mood        : {state.mood}",
        f"  attention   : {state.attention:.2f}",
        f"  loneliness  : {state.loneliness:.2f}",
        f"  boredom     : {state.boredom:.2f}",
        f"  curiosity   : {state.curiosity:.2f}",
    ]
    if at is not None:
        lines += [
            "活跃话题:",
            f"  topic_id    : {at.topic_id}",
            f"  topic_type  : {at.topic_type}",
            f"  topic_seed  : {at.topic_seed}",
            f"  user_engaged: {'是' if at.user_engaged else '否'}",
        ]
    else:
        lines.append("活跃话题: 无")
    return "\n".join(lines)


@debug_cmd.assign("set")
async def _set_state(field: str, value: str, state: MuikaState) -> str:
    if field not in _ALL_FIELDS:
        return f"未知字段 '{field}'，可修改的字段: {', '.join(sorted(_ALL_FIELDS))}"

    if field in _FLOAT_FIELDS:
        try:
            v = float(value)
            if not 0.0 <= v <= 1.0:
                return f"值必须在 0.0 ~ 1.0 之间，收到: {v}"
            setattr(state, field, v)
        except ValueError:
            return f"'{value}' 不是有效的浮点数"
    elif field in _STR_FIELDS:
        setattr(state, field, str(value))

    return f"[System] {field} = {getattr(state, field)}"


@debug_cmd.assign("reset")
async def _reset_topic(state: MuikaState) -> str:
    state.active_topic = None
    return "[System] 活跃话题已重置"
