"""
Standalone logger module for Muika-After-Story.
"""

from __future__ import annotations

import os
import sys
import time

from loguru import logger

_initialized = False


def init_logger(log_level: str, mas_log_only: bool) -> None:
    """使用当前进程的日志等级和命名空间过滤设置初始化日志。"""
    global _initialized
    if _initialized:
        return

    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.mkdir(log_dir)

    log_file_path = f"{log_dir}/{time.strftime('%Y-%m-%d')}.log"

    # 清除所有已有处理器；turn 供回合关联日志使用，无回合上下文时留空
    logger.configure(extra={"turn": ""})
    logger.remove()

    # 添加控制台处理器
    logger.add(
        sys.stdout,
        level=log_level,
        diagnose=True,
        format="<lvl>[{level}] {name}: {message}</lvl>",
        filter=lambda record: not mas_log_only or (record["name"] or "").startswith("muika"),
        colorize=True,
    )

    # 添加文件处理器
    logger.add(
        log_file_path,
        level="DEBUG",
        format="[{time:YYYY-MM-DD HH:mm:ss}] [{level}] {function}{extra[turn]}: {message}",
        encoding="utf-8",
        rotation="1 day",
        retention="7 days",
    )

    _initialized = True
