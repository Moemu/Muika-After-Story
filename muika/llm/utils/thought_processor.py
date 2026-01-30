import re


def general_processor(message: str) -> tuple[str, str]:
    thoughts_pattern = re.compile(r"<think>(.*?)</think>", re.DOTALL)
    match = thoughts_pattern.search(message)
    thoughts = match.group(1).replace("\n", "") if match else ""
    result = thoughts_pattern.sub("", message).strip()
    return thoughts, result
