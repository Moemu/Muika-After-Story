import re
from pathlib import Path
from typing import Iterable, Optional

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

    :return: 渲染后的提示词
    :raises TemplateNotFound: 找不到模板
    :raises RuntimeError: 模板渲染失败
    """
    if not template_name.endswith((".j2", ".jinja2")):
        template_name += ".jinja2"

    env = Environment(loader=FileSystemLoader(SEARCH_PATH), autoescape=True)

    render_data = templates_data.model_dump() if templates_data else {}

    try:
        template = env.get_template(template_name)
        prompt = template.render(render_data)
    except TemplateNotFound:
        logger.error(f"Template not found: {template_name}")
        raise
    except Exception as exc:
        logger.error(f"Template render failed for {template_name}: {exc}")
        raise RuntimeError(f"Template render failed for {template_name}: {exc}") from exc
    return re.sub(r"\n{3,}", "\n\n", prompt).strip()


def validate_template_configuration(template_names: Iterable[str]) -> None:
    """检查配置引用的模板是否存在且可解析。

    :param template_names: 配置中的模板名称
    :raises RuntimeError: 模板配置无效
    """
    env = Environment(loader=FileSystemLoader(SEARCH_PATH), autoescape=True)
    for configured_name in template_names:
        template_name = configured_name
        if not template_name.endswith((".j2", ".jinja2")):
            template_name += ".jinja2"
        try:
            env.get_template(template_name)
        except Exception as exc:
            raise RuntimeError(f"Invalid template configuration for {configured_name!r}: {exc}") from exc
