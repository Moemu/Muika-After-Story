import importlib
import sys
from importlib.util import find_spec
from typing import Optional

from nonebot import logger

from muika.config import get_model_config

from ._base import BaseLLM, EmbeddingModel
from ._config import EmbeddingConfig, ModelConfig
from ._dependencies import MODEL_DEPENDENCY_MAP, get_missing_dependencies
from .registry import get_embedding_class, get_llm_class

_embedding_instance: dict[EmbeddingConfig, EmbeddingModel] = {}
"""嵌入实例缓存"""


def load_model(config: Optional[ModelConfig] = None) -> BaseLLM:
    """
    获得一个 LLM 实例
    """
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


def load_embedding_model(config: EmbeddingConfig) -> EmbeddingModel:
    """
    获得一个嵌入模型实例，如果存在相同的配置，则返回缓存实例
    """
    provider = config.provider.lower()

    builtin_provider = f"muika.llm.embeddings.{provider}"
    if find_spec(builtin_provider) is not None:
        logger.debug(f"加载内嵌模型模块: {provider}...")
        importlib.import_module(builtin_provider)

    EmbeddingClass = get_embedding_class(provider)

    return _embedding_instance.setdefault(config, EmbeddingClass(config))
