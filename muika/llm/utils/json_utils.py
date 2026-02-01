import json
import re
from typing import Any, Union


def extract_json_from_text(text: str) -> Union[dict, list, Any]:
    """
    从文本中提取 JSON 对象。

    支持直接的 JSON 字符串，Markdown 代码块包裹的 JSON，以及嵌入在文本中的 JSON 对象。
    """
    cleaned_result = text.strip()

    # 1. 尝试直接解析
    try:
        return json.loads(cleaned_result, strict=False)
    except json.JSONDecodeError:
        pass

    json_str = None

    # 2. 尝试提取 Markdown 代码块
    pattern = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
    match = pattern.search(cleaned_result)
    if match:
        json_str = match.group(1)
    else:
        # 3. 尝试寻找最外层的大括号或方括号
        # 匹配 {...} 或者 [...]
        # 这里使用比较简单的正则，可能无法处理嵌套很深或者不平衡的情况，但在多数LLM输出中有效

        # 尝试匹配对象
        match_obj = re.search(r"(\{.*\})", cleaned_result, re.DOTALL)
        # 尝试匹配数组
        match_arr = re.search(r"(\[.*\])", cleaned_result, re.DOTALL)

        if match_obj and match_arr:
            # 如果两个都匹配到了，看谁更长或者根据位置（这里简单取最长的，通常是我们要的）
            # 或者看谁在外层。通常LLM只会输出一个主要的JSON。
            if len(match_obj.group(1)) > len(match_arr.group(1)):
                json_str = match_obj.group(1)
            else:
                json_str = match_arr.group(1)
        elif match_obj:
            json_str = match_obj.group(1)
        elif match_arr:
            json_str = match_arr.group(1)

    if not json_str:
        raise ValueError("无法在文本中识别 JSON 结构")

    try:
        return json.loads(json_str, strict=False)
    except json.JSONDecodeError as e:
        # 如果提取出来的部分还是无法解析
        raise ValueError(f"提取的内容不是有效的 JSON: {e}") from e
