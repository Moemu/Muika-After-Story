import re
from pathlib import Path
from typing import Optional

from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound

from muika.utils.logger import logger

from .model import PromptTemplatesData

_BUILTIN_TEMPLATES_DIR = Path(__file__).parent.parent / "builtin_templates"
SEARCH_PATH = ["./templates", _BUILTIN_TEMPLATES_DIR]


def generate_prompt_from_template(
    template_name: str,
    templates_data: Optional[PromptTemplatesData] = None,
) -> str:
    """
    从指定模板渲染提示词

    覆盖层 ``./templates`` 优先于包内 ``muika/builtin_templates``。

    :param template_name: 模板文件名
    :param templates_data: 渲染数据

    :return: 渲染后的提示词；模板不可用时返回空字符串
    """
    if not template_name.endswith((".j2", ".jinja2")):
        template_name += ".jinja2"

    env = Environment(loader=FileSystemLoader(SEARCH_PATH), autoescape=True)

    render_data = templates_data.model_dump() if templates_data else {}

    try:
        prompt = env.get_template(template_name).render(render_data)
    except TemplateNotFound:
        logger.error(f"Template not found: {template_name}")
        return ""
    except Exception as exc:
        logger.error(f"Template render failed for {template_name}: {exc}")
        return ""
    return re.sub(r"\n{3,}", "\n\n", prompt).strip()
