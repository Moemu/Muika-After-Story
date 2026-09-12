"""生成 Function Call 参数模型的 JSON Schema。"""

from pydantic.json_schema import GenerateJsonSchema


class FunctionCallJsonSchema(GenerateJsonSchema):
    def generate(self, schema, mode="validation"):
        json_schema = super().generate(schema, mode=mode)
        del json_schema["title"]
        for prop in json_schema.get("properties", {}).values():
            prop.pop("title", None)
        json_schema["additionalProperties"] = False
        return json_schema
