from __future__ import annotations

import os
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
