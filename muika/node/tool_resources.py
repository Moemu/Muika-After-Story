"""只转换参数中明确的资源引用，不复制任意本机路径。"""

from collections.abc import Callable

from pydantic import JsonValue


def map_resource_values(value: JsonValue, convert: Callable[[str], str]) -> JsonValue:
    if isinstance(value, str):
        return convert(value)
    if isinstance(value, list):
        return [map_resource_values(item, convert) for item in value]
    if isinstance(value, dict):
        return {key: map_resource_values(item, convert) for key, item in value.items()}
    return value
