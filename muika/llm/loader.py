import importlib
import sys
from importlib.util import find_spec
from typing import Optional

from muika.utils.logger import logger

from ._base import BaseLLM
from ._config import ModelConfig
from ._dependencies import MODEL_DEPENDENCY_MAP, get_missing_dependencies
from .registry import get_llm_class


def load_model(config: Optional[ModelConfig] = None) -> BaseLLM:
    """
    获得一个 LLM 实例
    """
    from muika.config import get_model_config  # 避免循环导入

    config = config or get_model_config()
    provider = config.provider.lower()

    try:
        # 如果是内置模型提供者，需要先导入
        # 否则视为已导入的插件
        builtin_provider = f"muika.llm.providers.{provider}"
        if find_spec(builtin_provider) is not None:
            logger.debug(f"加载内嵌模型模块: {provider}...")
            importlib.import_module(builtin_provider)

        # 注册之后，直接取类使用
        LLMClass = get_llm_class(provider)
    except (ImportError, ModuleNotFoundError) as e:
        logger.critical(f"加载模型加载器 '{provider}' 失败：{e}")
        dependencies = MODEL_DEPENDENCY_MAP.get(provider, [])
        missing = get_missing_dependencies(dependencies)
        if missing:
            install_command = "pip install " + " ".join(missing)
            logger.critical(f"缺少依赖库：{', '.join(missing)}\n请运行以下命令安装缺失项：\n\n{install_command}")
        sys.exit(1)

    return LLMClass(config)
