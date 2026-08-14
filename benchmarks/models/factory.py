"""候选模型解析与加载。

候选来源：models.yml 配置名 / ad-hoc 参数构造的 ModelConfig / smoke 脚本化。
ad-hoc 配置必须注册进 ``ModelConfigManager.configs``——否则 ``record_plugin_usage``
内的 ``get_name_from_config`` 找不到配置名会抛 ValueError。
"""

from __future__ import annotations

from typing import Any

from benchmarks.config import ModelSpec
from muika.config import get_model_config_manager
from muika.llm import ModelConfig, load_model


def resolve_candidates(
    model_names: list[str],
    adhoc: dict[str, Any] | None = None,
    smoke: bool = False,
) -> list[ModelSpec]:
    """把 CLI 参数解析为候选模型规格列表。

    :param model_names: models.yml 配置名；为空时回退到 default 配置
    :param adhoc: 单模型 ad-hoc 参数（provider / model_name / api_key / ...）
    :param smoke: 离线脚本化模式，返回单个 scripted 规格
    """
    if smoke:
        return [ModelSpec(name="scripted", scripted=True)]
    if adhoc:
        name = f"adhoc:{adhoc['provider']}:{adhoc.get('model_name') or 'default'}"
        return [ModelSpec(name=name, **adhoc)]
    names = list(model_names) or ["default"]
    return [ModelSpec(name=name) for name in names]


def build_inner_model(model_spec: ModelSpec) -> Any:
    """加载被录制的内层模型（真实 provider 或 ad-hoc）。"""
    return load_model(_resolve_config(model_spec))


def _resolve_config(model_spec: ModelSpec) -> ModelConfig:
    """把规格解析为 ModelConfig；ad-hoc 需注册进 manager（usage 包装器依赖配置名）。"""
    manager = get_model_config_manager()
    if model_spec.provider:
        config = ModelConfig(
            provider=model_spec.provider,
            model_name=model_spec.model_name or model_spec.name,
            api_key=model_spec.api_key or "",
            api_host=model_spec.api_host or "",
            temperature=model_spec.temperature if model_spec.temperature is not None else 0.75,
            top_p=model_spec.top_p if model_spec.top_p is not None else 0.95,
        )
        manager.configs.setdefault(model_spec.name, config)
        return config
    if model_spec.name == "default":
        return manager.get_model_config(None)
    return manager.get_model_config(model_spec.name)
