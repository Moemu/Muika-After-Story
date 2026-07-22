from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader
from jinja2.exceptions import TemplateNotFound
from nonebot import logger

from .model import PromptTemplatesData

SEARCH_PATH = ["./templates", Path(__file__).parent.parent / "builtin_templates"]

TEMPLATES_CONFIG_PATH = Path("./configs/templates.yml")


def load_templates_config() -> dict:
    """
    获取模板配置
    """
    if not TEMPLATES_CONFIG_PATH.exists():
        return {}
    try:
        with open(TEMPLATES_CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError:
        logger.warning("无法加载模板数据，请检查模板配置内容")
        return {}


def generate_prompt_from_template(template_name: str, templates_data: PromptTemplatesData) -> str:
    """
    获取提示词
    """
    env = Environment(loader=FileSystemLoader(SEARCH_PATH), autoescape=True)

    if not template_name.endswith((".j2", ".jinja2")):
        template_name += ".jinja2"
    try:
        template = env.get_template(template_name)
    except TemplateNotFound:
        logger.error(f"模板文件 {template_name} 未找到!")
        return ""

    prompt = template.render(templates_data.model_dump())

    return prompt
