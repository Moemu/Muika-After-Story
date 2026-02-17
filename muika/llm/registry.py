from typing import Dict, Type

from ._base import BaseLLM

LLM_REGISTRY: Dict[str, Type[BaseLLM]] = {}


def register(name: str):
    """
    注册一个 LLM 实现类

    :param name: LLM 实现名
    """

    def decorator(cls: Type[BaseLLM]):
        if issubclass(cls, BaseLLM):
            return LLM_REGISTRY.setdefault(name.lower(), cls)
        else:
            raise TypeError(f"Class {cls.__name__} must be a subclass of BaseLLM or EmbeddingModel")

    return decorator


def get_llm_class(name: str) -> Type[BaseLLM]:
    """
    获得一个 LLM 实现类

    :param name: LLM 实现名
    """
    if name.lower() not in LLM_REGISTRY:
        raise ValueError(f"未注册模型：{name}")

    return LLM_REGISTRY[name.lower()]
