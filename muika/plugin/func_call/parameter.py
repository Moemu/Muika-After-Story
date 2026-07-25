"""
定义 Function_call 插件的参数类
"""

from typing import Any

from pydantic import BaseModel
from pydantic.json_schema import GenerateJsonSchema


class FunctionCallJsonSchema(GenerateJsonSchema):
    def generate(self, schema, mode="validation"):
        json_schema = super().generate(schema, mode=mode)
        del json_schema["title"]
        for prop in json_schema.get("properties", {}).values():
            prop.pop("title", None)
        json_schema["additionalProperties"] = False
        return json_schema


class Parameter(BaseModel):
    """
    Function_call 插件参数对象
    """

    type: str
    """参数类型描述 string integer等"""
    description: str
    """参数描述"""
    default: Any = None
    """默认值"""
    properties: dict[str, Any] = {}
    """参数定义属性，例如最大值最小值等"""
    required: bool = False
    """是否必须"""

    def data(self) -> dict[str, Any]:
        """
        生成参数描述信息

        :return: 可用于 Function_call 的字典
        """
        return {
            "type": self.type,
            "description": self.description,
            **{key: value for key, value in self.properties.items() if value is not None},
        }
