"""保存设备角色与固定入口配置，凭据留在持久用户目录。"""

import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator
from tzlocal import get_localzone_name


class PluginBinding(BaseModel):
    """用户显式声明插件适用角色及所需工具，旧插件不自动进入多节点部署。"""

    model_config = ConfigDict(extra="forbid")
    module: str
    role: Literal["core", "device"]
    requires_tools: list[str] = Field(default_factory=list)


class NodeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    role: Literal["core", "executor", "bot"]
    address: str
    token: str = Field(repr=False, min_length=1)
    directory: Path
    ca_file: Path | None = None
    lease_seconds: float = Field(default=15, ge=1, le=300)
    plugins: list[PluginBinding] = Field(default_factory=list)

    @classmethod
    def read(cls, path: Path) -> "NodeProfile":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


class ServerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = "server"
    host: str = "127.0.0.1"
    port: int = Field(default=8766, ge=1, le=65535)
    public_address: str
    directory: Path
    database: Path
    certificate: Path | None = None
    private_key: Path | None = None
    embedded_core: bool = True
    ca_file: Path | None = None
    timezone: str = Field(default_factory=get_localzone_name)

    @model_validator(mode="after")
    def require_tls(self) -> "ServerProfile":
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("State service timezone is invalid.") from error
        if (self.certificate is None) != (self.private_key is None):
            raise ValueError("Provide both the TLS certificate and private key.")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and self.certificate is None:
            raise ValueError("Public listeners require TLS. Bind to loopback behind a TLS reverse proxy.")
        return self

    @classmethod
    def read(cls, path: Path) -> "ServerProfile":
        return cls.model_validate_json(path.read_text(encoding="utf-8"))


def write_private_json(path: Path, value: BaseModel | dict) -> None:
    """原子保存配置，并在支持的系统上只允许当前用户读取。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as file:
            os.chmod(temporary, 0o600)
            file.write(
                value.model_dump_json(indent=2)
                if isinstance(value, BaseModel)
                else json.dumps(value, ensure_ascii=False, indent=2)
            )
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
