"""Bot 层配置，独立于 Core 的 MASConfig。"""

from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotConfig(BaseSettings):
    """Bot 配置；环境变量优先于当前目录的 .env 文件。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    master_id: str = ""
    """主人账号 ID；留空时沿用 NoneBot 的超级用户隐式回退。"""
    core_ws_url: str = "ws://127.0.0.1:8765/ws"
    """Core 或 Gateway 的 Bot WebSocket 地址，使用 /ws 路径。"""
    core_fallback_urls: list[str] = Field(default_factory=list)
    """主入口不可用时尝试的备用地址。环境变量使用 JSON 数组；主入口恢复后自动返回。"""
    ipc_secret: str = ""
    """IPC 预共享密钥，必须与连接的 Core 或 Gateway 一致。Bot 不自动生成或写回密钥。"""
    client_name: str = ""
    """当前 Bot 实例的唯一名称，如 qq-desktop。留空时使用适配器名称或默认名称。
    多个 Bot 连接同一入口时，必须设置不同名称。"""
    input_timeout: int = 0
    """合并连续消息前的等待时间，单位为秒。0 表示不额外等待。"""
    data_dir: Path = Path("./data")
    """Bot 数据目录，用于保存用户协议同意记录、聊天附件和下载文件。"""
    proxy: str | None = Field(default=None, validation_alias=AliasChoices("proxy", "telegram_proxy"))
    """Telegram 附件下载使用的代理地址，如 http://127.0.0.1:7890。
    兼容旧 TELEGRAM_PROXY 配置；同时设置时，PROXY 优先。"""
    log_level: str = "INFO"
    """控制台日志等级。文件日志固定记录 DEBUG 及以上等级。"""
    mas_log_only: bool = False
    """控制台是否仅输出 muika 命名空间的日志。文件日志不受此选项限制。"""


bot_config = BotConfig()
