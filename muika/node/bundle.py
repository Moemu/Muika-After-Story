"""复制人格配置和文本资源，保留每台设备的权限与原始用户文件。"""

import hashlib
from pathlib import Path
from typing import Literal

import yaml
from jinja2 import Environment, TemplateError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from muika.config import mas_config
from muika.llm._config import ModelConfig
from muika.node.config import write_private_json

COGNITIVE_SETTINGS = (
    "persona_template",
    "agent_template",
    "agent_model",
    "session_summarize_model",
    "heartbeat_intensity",
)


class CognitiveSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_template: str
    agent_template: str
    agent_model: str | None = None
    session_summarize_model: str | None = None
    heartbeat_intensity: Literal["off", "low", "medium", "high"] = "off"


class CognitiveBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    settings: CognitiveSettings
    files: dict[str, str] = Field(default_factory=dict)

    def apply_settings(self) -> None:
        """应用人格选项，保持本机行动权限和文件授权不变。"""
        mas_config.persona_template = self.settings.persona_template
        mas_config.agent_template = self.settings.agent_template
        mas_config.agent_model = self.settings.agent_model
        mas_config.session_summarize_model = self.settings.session_summarize_model
        mas_config.heartbeat_intensity = self.settings.heartbeat_intensity

    def validate_configuration(self) -> None:
        """发布前验证模型配置和所选模板，拒绝不可恢复的认知配置。"""
        models = yaml.safe_load(self.files.get("configs/models.yml", ""))
        if not isinstance(models, dict) or not models:
            raise ValueError("Cognitive configuration needs a non-empty configs/models.yml.")
        for config in models.values():
            try:
                ModelConfig.model_validate(config)
            except ValidationError as exc:
                raise ValueError("Cognitive model configuration is invalid; check the local model file.") from exc
        for selected in (self.settings.persona_template, self.settings.agent_template):
            name = selected if selected.endswith((".j2", ".jinja2")) else selected + ".jinja2"
            path = Path(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Selected cognitive template path is invalid.")
            text = self.files.get("templates/" + name)
            if text is None:
                builtin = Path(__file__).resolve().parents[1] / "builtin_templates" / name
                if not builtin.is_file():
                    raise ValueError(f"Selected cognitive template is missing: {name}")
                text = builtin.read_text(encoding="utf-8")
            try:
                Environment(autoescape=True).parse(text)
            except TemplateError as exc:
                raise ValueError(f"Cognitive template syntax is invalid: {name}") from exc

    @field_validator("files")
    @classmethod
    def safe_files(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in name
                or not (
                    name in {"configs/models.yml", "configs/topics.yml"}
                    or name.startswith("templates/")
                    or name.startswith("configs/skills/")
                )
            ):
                raise ValueError("Bundle contains an invalid configuration path.")
        if len(value) > 1000 or sum(len(text.encode()) for text in value.values()) > 1024 * 1024:
            raise ValueError("Cognitive bundle exceeds the 1 MiB limit.")
        return value

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    @classmethod
    def capture(cls, directory: Path) -> "CognitiveBundle":
        files = {}
        for name in ("configs/models.yml", "configs/topics.yml"):
            path = directory / name
            if path.is_file():
                files[name] = path.read_text(encoding="utf-8")
        for name in ("templates", "configs/skills"):
            root = directory / name
            if root.is_dir():
                for path in sorted(root.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        files[path.relative_to(directory).as_posix()] = path.read_text(encoding="utf-8")
        settings = {
            name: str(value) if value is not None else None
            for name, value in mas_config.model_dump().items()
            if name in COGNITIVE_SETTINGS
        }
        return cls(settings=CognitiveSettings.model_validate(settings), files=files)

    def materialize(self, directory: Path) -> None:
        """只更新专用副本，不修改设备工作目录中的用户文件。"""
        directory.mkdir(parents=True, exist_ok=True)
        directory = directory.resolve()
        for name in self.files:
            if not (directory / name).resolve().is_relative_to(directory):
                raise ValueError("Cognitive cache contains a path outside its directory.")
        if (directory / "bundle.json").is_symlink():
            raise ValueError("Cognitive manifest cannot be a symbolic link.")
        manifest = directory / "bundle.json"
        if manifest.is_file():
            previous = CognitiveBundle.model_validate_json(manifest.read_text(encoding="utf-8"))
            for name in previous.files.keys() - self.files.keys():
                if not (directory / name).resolve().is_relative_to(directory):
                    raise ValueError("Previous cognitive cache contains a path outside its directory.")
                (directory / name).unlink(missing_ok=True)
        for name, text in self.files.items():
            path = directory / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            path.chmod(0o600)
        write_private_json(manifest, self)
