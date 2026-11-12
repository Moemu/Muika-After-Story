"""Bot 配置的来源、主人身份与旧配置兼容性。"""

import runpy
from pathlib import Path

import pytest


@pytest.fixture
def bot_config_type(monkeypatch):
    for name in ("MASTER_ID", "SUPERUSERS", "PROXY", "TELEGRAM_PROXY", "IPC_SECRET"):
        monkeypatch.delenv(name, raising=False)
    path = Path(__file__).resolve().parents[1] / "muika_bot" / "config.py"
    return runpy.run_path(str(path))["BotConfig"]


def test_bot_defaults_do_not_generate_core_secret(bot_config_type, tmp_path):
    config = bot_config_type(_env_file=None)
    assert config.core_ws_url == "ws://127.0.0.1:8765/ws"
    assert config.core_fallback_urls == []
    assert config.ipc_secret == ""
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    ("master_id", "superusers", "expected"),
    [("", ["test-player"], "test-player"), ("player", ["other"], "player"), ("", ["z", "a"], "a")],
)
def test_master_id_and_json_superusers(bot_config_type, monkeypatch, master_id, superusers, expected):
    import json

    monkeypatch.setenv("MASTER_ID", master_id)
    monkeypatch.setenv("SUPERUSERS", json.dumps(superusers))
    assert bot_config_type(_env_file=None).master_id == expected


def test_bot_dotenv_and_environment_precedence(bot_config_type, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        'MASTER_ID=player\nCORE_WS_URL=ws://primary/ws\nCORE_FALLBACK_URLS=["ws://local/ws"]\n'
        "CLIENT_NAME=local-qq\nINPUT_TIMEOUT=3\nDATA_DIR=bot-data\nIPC_SECRET=shared-secret\n",
        encoding="utf-8",
    )
    config = bot_config_type(_env_file=env)
    assert config.master_id == "player"
    assert config.core_ws_url == "ws://primary/ws"
    assert config.core_fallback_urls == ["ws://local/ws"]
    assert config.client_name == "local-qq"
    assert config.input_timeout == 3
    assert config.data_dir == Path("bot-data")
    assert config.ipc_secret == "shared-secret"
    monkeypatch.setenv("CORE_WS_URL", "ws://override/ws")
    assert bot_config_type(_env_file=env).core_ws_url == "ws://override/ws"


@pytest.mark.parametrize("proxy", [None, "http://current:7890"])
def test_bot_legacy_proxy_and_explicit_override(bot_config_type, monkeypatch, proxy):
    monkeypatch.setenv("TELEGRAM_PROXY", "http://legacy:7890")
    if proxy:
        monkeypatch.setenv("PROXY", proxy)
    assert bot_config_type(_env_file=None).proxy == (proxy or "http://legacy:7890")
