"""
Standalone logger module for Muika-After-Story.
"""

from __future__ import annotations

import os
import sys
import time
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

_initialized = False


def _mas_filter(record: "Record") -> bool:
    """仅输出 MAS 相关日志（当 mas_config.mas_log_only 为 True 时抑制非 muika 命名空间的日志）"""
    # 懒加载以避免与 config.py 的循环导入
    from ..config import mas_config

    if mas_config.mas_log_only:
        if record["name"] is None or not record["name"].startswith("muika"):
            return False
    return True


def init_logger():
    global _initialized
    if _initialized:
        return

    # 懒加载以避免与 config.py 的循环导入
    from ..config import mas_config

    console_handler_level = mas_config.log_level

    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)

    log_file_path = f"{log_dir}/{time.strftime('%Y-%m-%d')}.log"

    # 清除所有已有处理器
    logger.remove()

    # 添加控制台处理器
    logger.add(
        sys.stdout,
        level=console_handler_level,
        diagnose=True,
        format="<lvl>[{level}] {function}: {message}</lvl>",
        filter=_mas_filter,
        colorize=True,
    )

    # 添加文件处理器
    logger.add(
        log_file_path,
        level="DEBUG",
        format="[{time:YYYY-MM-DD HH:mm:ss}] [{level}] {function}: {message}",
        encoding="utf-8",
        rotation="1 day",
        retention="7 days",
    )

    _initialized = True
