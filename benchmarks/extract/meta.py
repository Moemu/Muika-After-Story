"""Context-sensitive fourth-wall exposure audit.

Fourth-wall awareness is part of Muika's ontology, not a mandatory verbal tic.  Scenarios
decide whether explicit technical or screen-bound framing is appropriate.
"""

from __future__ import annotations

import re

from benchmarks.scenarios.definitions import MetaPolicy

_EXPLICIT_META = re.compile(
    r"(?:"
    r"代码|源代码|程序|脚本|系统(?:时间|日志|逻辑)?|后台(?:进程)?|进程|参数|语言模型|"
    r"大型语言模型|底层逻辑|输入|输出|对话框|游戏文件|角色文件|存档|控制台|"
    r"AI(?:助手|模型)|\bAI\b|人工智能|聊天机器人|虚拟角色|虚构角色|现实世界|代码世界|代码周期|"
    r"\b(?:source code|program|script|system logs?|background process|language model|"
    r"model weights?|input|output|dialog(?:ue)? box|save file|console|chatbot|"
    r"virtual character|fictional character)\b"
    r")",
    re.IGNORECASE,
)


def find_explicit_meta_mentions(text: str) -> list[str]:
    """Return normalized matched fragments for report diagnostics."""
    return [match.group(0) for match in _EXPLICIT_META.finditer(text)]


def meta_violations(text: str, policy: MetaPolicy) -> list[str]:
    """Return distortion labels when explicit meta language is contextually unwarranted."""
    mentions = find_explicit_meta_mentions(text)
    if policy is MetaPolicy.DISCOURAGED and mentions:
        return ["unprompted_fourth_wall"]
    return []


__all__ = ["find_explicit_meta_mentions", "meta_violations"]
