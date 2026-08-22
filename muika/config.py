from __future__ import annotations

import atexit
import os
import secrets
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, List, Literal, Optional

import yaml as yaml_
from pydantic import AliasChoices, Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from muika.utils.logger import init_logger, logger

from .llm import ModelConfig

MODELS_CONFIG_PATH = Path("configs/models.yml").resolve()

BUILTIN_SKILLS_PATH = Path("configs/skills").resolve()
"""内置技能目录，始终会被扫描"""

USER_SKILL_PATHS = (Path.home() / ".agents" / "skills", Path.home() / ".claude" / "skills")
"""用户级技能目录，仅在 load_user_skills 启用时扫描"""

_model_config_manager: Optional["ModelConfigManager"] = None


class MASConfig(BaseSettings):
    master_id: str = ""
    """对话目标ID。"""
    max_memory_records: int = 100
    """最大记忆记录数(最近的N条对话)"""
    persona_template: str = "Muika.md.jinja2"
    """默认人格模板"""
    agent_template: str = "Muika.agent.jinja2"
    """行动半身（Agent）模板：Muika 执行 <agent>...</agent> 内联命令时的系统提示"""

    input_timeout: int = 0
    """输入等待时间"""
    enable_embedding_cache: bool = True
    """启用嵌入缓存"""

    log_level: str = "INFO"
    """日志等级"""
    mas_log_only: bool = False
    """仅输出 MAS 相关日志（不输出 NoneBot 核心日志）"""
    telegram_proxy: Optional[str] = None
    """telegram代理，这个配置项用于获取图片时使用"""

    butler_model: Optional[str] = None
    """管家 Agent 所用模型的配置名。留空则与核心模型共享 default 配置"""
    session_summarize_model: Optional[str] = None
    """会话总结 Agent 所用模型的配置名，建议使用与核心模型相同型号或其量化版本。留空则使用管家模型"""

    fs_allowed_paths: List[str] = []
    """文件操作白名单目录列表。空列表时文件系统工具全部禁用。
    示例: ["D:/Documents", "D:/Downloads"]"""

    enable_file_write: bool = False
    """开启文件写入/删除操作（Tier 2）。需同时在 fs_allowed_paths 中声明目标目录。"""

    enable_code_execution: bool = False
    """开启 Python 子进程代码执行能力。存在一定安全风险，请确认后再启用。"""

    enable_shell_execution: bool = False
    """开启 Shell 命令执行（PowerShell/Bash/Cmd）。存在一定安全风险，请确认后再启用。"""

    load_user_skills: bool = False
    """是否额外扫描用户级技能目录（~/.agents/skills 与 ~/.claude/skills）。
    内置技能目录 configs/skills 始终会被扫描。"""

    core_ws_url: str = "ws://127.0.0.1:8765/ws"
    """Core 进程的 WebSocket 地址。Bot 通过此地址连接 Core。"""

    data_dir: Path = Path("./data")
    """数据目录路径，用于存储连接记录等运行时数据。默认为当前工作目录。"""

    plugins_dir: str = "plugins"
    """插件目录路径。Core 启动时从此目录递归加载所有 MAS 插件。"""

    client_name: str = ""
    """适配器唯一名称。用于多适配器场景下标识当前 Bot 实例的身份。
    例如 ``"qq-desktop"``, ``"qq-phone"``。留空时自动分配。"""

    ipc_secret: str = ""
    """IPC 通信的预共享密钥。Bot 连接 Core 时需携带此 Token。
    留空时 Core 启动会自动生成并写入 .env 文件。"""

    heartbeat_intensity: Literal["low", "medium", "high", "off"] = Field(
        default="off",
        validation_alias=AliasChoices("HEART_INTENSITY", "heartbeat_intensity"),
    )
    """内心思考强度"""

    @field_validator("master_id")
    def validate_master_id(cls, v):
        if v:
            return v

        superusers = os.getenv("SUPERUSERS")
        if not superusers:
            raise ValidationError("未设置 master_id，Muika 将无法正常工作！请在配置文件中设置 master_id")

        try:
            v = str(list(superusers)[0])
            return v
        except Exception as exc:
            logger.error(exc)
            raise ValidationError("未设置 master_id，Muika 将无法正常工作！请在配置文件中设置 master_id")

    @staticmethod
    def generate_ipc_secret() -> str:
        """
        创建一个新的 IPC 密钥

        :return: 有效的 IPC 密钥
        """
        token = secrets.token_urlsafe(32)

        env_path = Path(".env")
        try:
            env_data = env_path.read_text(encoding="utf-8")
        except UnicodeEncodeError:
            env_data = env_path.read_text(encoding="gbk")

        env_data = env_data.strip()
        env_data += f"\nIPC_SECRET={token}\n"

        env_path.write_text(env_data, encoding="utf-8")

        logger.info(f"[Config] Auto-generated IPC secret and wrote to {env_path}")
        return token

    @field_validator("ipc_secret")
    def validate_ipc_secret(cls, v):
        if v:
            return v

        init_logger()
        logger.warning("未设置 ipc_secret, 将随机生成一个并写入 .env 文件中")
        return cls.generate_ipc_secret()

    model_config = SettingsConfigDict(extra="allow", env_file=".env")


mas_config = MASConfig()


# 各强度档位的模型采样覆写表
# high 收紧 temperature 并提高重复/存在惩罚，符合"高强度思考更专注"的语义
HEART_INTENSITY_SAMPLING: dict[str, dict[str, Any] | None] = {
    "off": None,
    "low": {"temperature": 0.8, "top_p": 0.9},
    "medium": {},
    "high": {
        "temperature": 0.7,
        "top_p": 0.92,
        "repetition_penalty": 1.15,
        "presence_penalty": 0.35,
        "frequency_penalty": 0.2,
    },
}


class ConfigFileHandler(FileSystemEventHandler):
    """配置文件变化处理器"""

    def __init__(self, path: Path, callback: Callable):
        self.path = path
        self.callback = callback
        self.last_modified = time.time()
        # 防止一次修改触发多次回调
        self.cooldown = 1  # 冷却时间（秒）

    def on_modified(self, event):
        if not os.path.samefile(event.src_path, self.path):
            return

        current_time = time.time()
        if current_time - self.last_modified > self.cooldown:
            self.last_modified = current_time
            self.callback()


class ModelConfigManager:
    """模型配置管理器"""

    _instance: Optional["ModelConfigManager"] = None
    _lock = threading.Lock()
    _initialized: bool
    configs: dict[str, ModelConfig]

    def __new__(cls):
        """确保实例在单例模式下运行"""
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(ModelConfigManager, cls).__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return

        self.configs: dict[str, ModelConfig] = {}
        """所有模型配置"""
        self.current_config: Optional[ModelConfig] = None
        """默认模型配置（非主 MAS 使用模型）"""
        self.observer: Optional[BaseObserver] = None
        """文件监视器"""
        self._listeners: List[Callable] = []
        """监听器列表"""

        self._load_configs()
        self._start_file_watcher()

        # Heart 内心思考强度：运行时经 set_heart_intensity 切换，仅存内存；
        # _heart_base_config 为覆写前的基准配置，用于回到默认档时恢复。
        self.heart_intensity: Literal["low", "medium", "high", "off"] = mas_config.heartbeat_intensity
        self._heart_base_config: Optional[ModelConfig] = (
            self.current_config.model_copy(deep=True) if self.current_config else None
        )
        if self.heart_intensity != "medium" and HEART_INTENSITY_SAMPLING.get(self.heart_intensity):
            self.current_config = self._compose()

        self._initialized = True

    def _load_configs(self):
        """
        加载配置文件，并设置默认模型
        """
        if not os.path.isfile(MODELS_CONFIG_PATH):
            raise FileNotFoundError("configs/models.yml 不存在！请先创建")

        with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
            configs_dict = yaml_.safe_load(f)

        if not configs_dict:
            raise ValueError("configs/models.yml 为空，请先至少定义一个模型配置")

        self.configs = {}
        for name, config in configs_dict.items():
            self.configs[name] = ModelConfig(**config)
            if config.get("default"):
                self.current_config = self.configs[name]

        if not self.current_config and self.configs:
            # 如果没有指定默认配置，使用第一个
            self.current_config = next(iter(self.configs.values()))

    def _start_file_watcher(self):
        """启动文件监视器"""
        if self.observer is not None:
            self.observer.stop()

        self.observer = Observer()
        event_handler = ConfigFileHandler(MODELS_CONFIG_PATH, self._on_config_changed)
        self.observer.schedule(event_handler, str(MODELS_CONFIG_PATH.parent), recursive=False)
        self.observer.start()

    def _on_config_changed(self):
        """配置文件变化时的回调函数"""
        try:
            # old_configs = self.configs.copy()
            old_default = self.current_config.model_copy() if self.current_config else None

            self._load_configs()

            # Heart 基准随配置文件重载更新，随后叠加当前强度覆写
            self._heart_base_config = self.current_config.model_copy(deep=True) if self.current_config else None
            self.current_config = self._compose()

            # 通知所有注册的监听器
            for listener in self._listeners:
                listener(self.current_config, old_default)

        except Exception as e:
            logger.error(f"重新加载配置文件失败: {e}")

    def register_listener(self, listener: Callable):
        """
        注册配置变化监听器

        :param listener: 回调函数，接收两个参数：新的默认配置和旧的默认配置
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unregister_listener(self, listener: Callable):
        """取消注册配置变化监听器"""
        if listener in self._listeners:
            self._listeners.remove(listener)

    def get_model_config(self, model_config_name: Optional[str] = None) -> ModelConfig:
        """获取指定模型的配置"""
        if model_config_name in [None, ""]:
            if not self.current_config:
                raise ValueError("没有找到默认模型配置！请确保存在至少一个有效的配置项！")
            return self.current_config

        elif model_config_name in self.configs:
            return self.configs[model_config_name]

        else:
            logger.warning(f"指定的模型配置 '{model_config_name}' 不存在！")
            raise ValueError(f"指定的模型配置 '{model_config_name}' 不存在！")

    def _compose(self) -> Optional[ModelConfig]:
        """
        将当前 Heart 强度的采样覆写叠加到基准配置上，返回生效配置

        :return: 叠加后的 ModelConfig；基准缺失时返回 None
        """
        base = self._heart_base_config or self.current_config
        if base is None:
            return self.current_config
        overrides = HEART_INTENSITY_SAMPLING.get(self.heart_intensity) or {}
        if not overrides:
            return base
        return base.model_copy(update=overrides)

    def set_heart_intensity(self, level: Literal["low", "medium", "high", "off"]) -> None:
        """
        切换内心独白（Heart）强度：将对应采样覆写叠加到全局 ModelConfig 实例上。

        首次切换会把当前配置缓存为基准；之后切换基于基准重新叠加，
        回落默认档（medium）即恢复基准配置。随后通知监听器（brain 据此重建模型）。

        :param level: 目标强度等级
        :raise ValueError: 未知强度等级
        """
        if level not in HEART_INTENSITY_SAMPLING:
            raise ValueError(f"Unknown heart intensity: {level}")

        self.heart_intensity = level
        old = self.current_config
        if self._heart_base_config is None and old is not None:
            self._heart_base_config = old.model_copy(deep=True)
        self.current_config = self._compose()

        # 通知所有注册的监听器
        for listener in self._listeners:
            listener(self.current_config, old)

    def change_current_config(self, config: ModelConfig):
        old_default = self.current_config.model_copy() if self.current_config else None
        self.current_config = config
        # 切换模型后，Heart 基准更新，并保持当前强度覆写生效
        self._heart_base_config = config.model_copy(deep=True)
        self.current_config = self._compose()

        # 通知所有注册的监听器
        for listener in self._listeners:
            listener(self.current_config, old_default)

    def get_name_from_config(self, config: ModelConfig) -> str:
        """
        从配置对象获取配置名称

        :param config: ModelConfig 实例
        :return: 相应配置在配置文件中的配置名

        :raise ValueError: 当配置不存在时
        """
        for key, value in self.configs.items():
            if value == config:
                return key

        raise ValueError("指定的配置对象不存在")

    def stop_watcher(self):
        """停止文件监视器"""
        if self.observer is None:
            return

        self.observer.stop()
        self.observer.join()


def get_model_config_manager() -> ModelConfigManager:
    global _model_config_manager
    if _model_config_manager is None:
        _model_config_manager = ModelConfigManager()
        atexit.register(_model_config_manager.stop_watcher)
    return _model_config_manager


def get_model_config(model_config_name: Optional[str] = None) -> ModelConfig:
    """
    从配置文件 `configs/models.yml` 中获取指定模型的配置对象

    :param model_config_name: (可选)模型配置名称。若为空，则先寻找配置了 `default: true` 的首个配置项，若失败就再寻找首个配置项

    :raise FileNotFoundError: 配置文件不存在
    """
    model_config_manager = get_model_config_manager()
    return model_config_manager.get_model_config(model_config_name)


@lru_cache(64)
def get_name_from_config(config: Optional[ModelConfig] = None) -> str:
    model_config_manager = get_model_config_manager()

    config = config or get_model_config()

    return model_config_manager.get_name_from_config(config)
