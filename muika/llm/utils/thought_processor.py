import re


def general_processor(message: str) -> tuple[str, str]:
    thoughts_pattern = re.compile(r"<think\s*>(.*?)(?:</think\s*>|$)", re.DOTALL | re.IGNORECASE)
    match = thoughts_pattern.search(message)
    thoughts = match.group(1).replace("\n", "") if match else ""
    # qwen 思考模式下可能把闭合标记字面写进正文（无开标签），成对剥离后补删孤儿闭合
    result = re.sub(r"</think\s*>", "", thoughts_pattern.sub("", message)).strip()
    return thoughts, result
