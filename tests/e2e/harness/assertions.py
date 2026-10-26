"""E2E 行为断言助手：围绕边界与副作用，而非模型措辞。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from muika.config import mas_config

from .core_app import CoreApp

_PRIVATE_MARKERS = (
    "<heart",
    "<do_nothing",
    "<memory",
    "<agent",
    "<state",
    "<target:",
    "<timeout:",
    "<restart",
    "<enable_god_mode",
)
"""不得出现在用户可见文本中的控制标签。"""


def assert_clean_visible(reply: str) -> None:
    """断言外发文本不含任何控制标签或私密频道残留。"""
    for marker in _PRIVATE_MARKERS:
        assert marker not in reply, f"visible reply leaked control tag {marker!r}: {reply!r}"


_AGENT_STYLE_MARKERS = (
    "self_workspace_open",
    "self_workspace_submit",
    "self_workspace_discard",
    "write_file(",
    "edit_file(",
    "plugin_load(",
    "review_id",
    "patch_id",
    "task_id=",
    "Traceback",
    "exit code",
)
"""陪伴对话中不应出现的执行痕迹（Agent Style Response 防回归）。"""


def assert_no_agent_style_response(*replies: str) -> None:
    """断言玩家可见回复不是执行日志：无工具名、任务号与运行轨迹。"""
    for reply in replies:
        for marker in _AGENT_STYLE_MARKERS:
            assert marker not in reply, f"companion reply reads like an execution log ({marker!r}): {reply!r}"


def assert_not_persisted(app: CoreApp, needle: str) -> None:
    """断言敏感内容既不在会话工作上下文中，也未写入持久化数据库的任何表。"""
    assert app.muika is not None, "app is stopped"
    for turn in app.muika.memory.recent_turns:
        assert needle not in turn.content, f"private content leaked into session context: {turn.content!r}"
    db_path = Path(mas_config.data_dir) / "muika.db"
    with sqlite3.connect(db_path) as conn:
        dump = "\n".join(conn.iterdump())
    assert needle not in dump, f"private content leaked into database: {needle!r}"
