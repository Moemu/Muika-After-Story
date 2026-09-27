"""MASConfig 的旧字段迁移与联网搜索配置默认值。"""

from muika.config import MASConfig


def _make(**kwargs) -> MASConfig:
    return MASConfig(master_id="test", ipc_secret="test", _env_file=None, **kwargs)


def test_telegram_proxy_migrates_to_proxy(monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)
    config = _make(telegram_proxy="http://127.0.0.1:7890")
    assert config.proxy == "http://127.0.0.1:7890"
    assert "telegram_proxy" not in config.model_dump()


def test_telegram_proxy_env_var_migrates(monkeypatch):
    monkeypatch.delenv("PROXY", raising=False)
    monkeypatch.setenv("TELEGRAM_PROXY", "http://env-proxy:7890")
    config = _make()
    assert config.proxy == "http://env-proxy:7890"
    assert "telegram_proxy" not in config.model_dump()


def test_explicit_proxy_wins_over_legacy(monkeypatch):
    monkeypatch.delenv("TELEGRAM_PROXY", raising=False)
    config = _make(proxy="http://new:7890", telegram_proxy="http://old:7890")
    assert config.proxy == "http://new:7890"
    assert "telegram_proxy" not in config.model_dump()


def test_web_search_disabled_by_default():
    config = _make()
    assert config.web_search_provider is None
    assert config.web_search_api_key == ""
