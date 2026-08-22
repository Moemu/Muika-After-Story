"""``HEART_INTENSITY_SAMPLING`` 采样覆写表与 ``ModelConfigManager.set_heart_intensity`` 测试。

用 ``__new__`` 构造管理器，绕开 ``__init__`` 触发的文件 watcher / config 加载。
"""

from muika.config import HEART_INTENSITY_SAMPLING, ModelConfigManager
from muika.llm import ModelConfig


def _bare_manager() -> ModelConfigManager:
    """无副作用的最小 ModelConfigManager：不启动 watcher，不加载 models.yml。"""
    mgr = ModelConfigManager.__new__(ModelConfigManager)
    mgr.configs = {}
    mgr.current_config = ModelConfig(provider="openai", model_name="test")
    mgr.observer = None
    mgr._listeners = []
    mgr._heart_base_config = mgr.current_config.model_copy(deep=True)
    mgr.heart_intensity = "medium"
    return mgr


def test_sampling_keys_are_model_config_fields():
    for level, overrides in HEART_INTENSITY_SAMPLING.items():
        assert level in ("low", "medium", "high", "off")
        if overrides is None:
            continue
        for key in overrides:
            assert key in ModelConfig.model_fields, f"{level} 覆写含非法字段 {key!r}"


def test_medium_has_no_override():
    assert HEART_INTENSITY_SAMPLING["medium"] == {}


def test_set_heart_intensity_applies_overrides():
    mgr = _bare_manager()
    mgr.set_heart_intensity("high")
    assert mgr.heart_intensity == "high"
    assert mgr.current_config.temperature == HEART_INTENSITY_SAMPLING["high"]["temperature"]
    assert mgr.current_config.presence_penalty == HEART_INTENSITY_SAMPLING["high"]["presence_penalty"]


def test_set_heart_intensity_back_to_medium_restores_base():
    mgr = _bare_manager()
    base_temp = mgr.current_config.temperature
    mgr.set_heart_intensity("high")
    assert mgr.current_config.temperature != base_temp
    mgr.set_heart_intensity("medium")
    assert mgr.current_config.temperature == base_temp


def test_set_heart_intensity_unknown_raises():
    mgr = _bare_manager()
    try:
        mgr.set_heart_intensity("ultra")  # type: ignore[arg-type]
    except ValueError:
        return
    raise AssertionError("未知强度应抛 ValueError")
