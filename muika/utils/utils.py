from __future__ import annotations

import os
import re
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from mimetypes import guess_type
from pathlib import Path
from typing import Optional

import fleep

from muika.models import Resource

from .logger import logger


def get_version() -> str:
    """
    获取当前版本号
    """
    package_name = "muika-after-story"

    try:
        return version(package_name)
    except PackageNotFoundError:
        pass

    return "Unknown"


def guess_mimetype(resource: Resource) -> Optional[str]:
    """
    尝试获取 minetype 类型
    """
    if resource.url:
        return guess_type(resource.url)[0]

    elif resource.path and os.path.exists(resource.path):
        try:
            with open(resource.path, "rb") as file:
                header = file.read(128)
        except Exception as e:
            logger.warning(f"读取文件头时发生错误: {e} | {resource}")
            header = None

    elif resource.raw:
        try:
            header = resource.raw.read(128) if isinstance(resource.raw, BytesIO) else resource.raw[:128]
        except Exception as e:
            logger.warning(f"读取原始数据头时发生错误: {e} | {resource}")
            return None

    else:
        logger.warning(f"此实例无法获取元类型! {resource}")
        return None

    if header:
        info = fleep.get(header)

        # fleep 对于文档类文件失准，如果有后缀就不判断了
        if info.type and info.type[0] == "document" and Path(resource.path).suffix:
            return None

        if info.mime:
            return info.mime[0]
    elif resource.path:
        return guess_type(resource.path)[0]

    return None


def clamp(value: float, min_value: float, max_value: float) -> float:
    """限制值在最小和最大值之间"""
    return max(min_value, min(value, max_value))


_DURATION_UNITS = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "m": 60.0,
    "min": 60.0,
    "minute": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
}


def parse_duration(text: str) -> Optional[float]:
    """解析时长字符串为秒数，如 "10s"、"5min"、"2h"；无法解析时返回 None。"""
    if not text:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text.strip().lower())
    if not match:
        return None
    unit = match.group(2)
    if unit != "s" and unit.endswith("s"):
        unit = unit[:-1]  # 支持 "mins"/"hours"/"days" 等复数写法
    multiplier = _DURATION_UNITS.get(unit)
    if multiplier is None:
        return None
    return float(match.group(1)) * multiplier


def format_duration(seconds: float) -> str:
    """将秒数格式化为人类可读时长，如 "30 seconds"、"5 minutes"、"2 hours"。"""
    seconds = max(0.0, seconds)
    if seconds < 60:
        value, unit = round(seconds), "second"
    elif seconds < 3600:
        value, unit = round(seconds / 60), "minute"
    elif seconds < 86400:
        value, unit = round(seconds / 3600), "hour"
    else:
        value, unit = round(seconds / 86400), "day"
    return f"{value} {unit}{'s' if value != 1 else ''}"
