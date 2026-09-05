"""共享协议正文、同意状态和版本校验。"""

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class AgreementContent:
    title: str
    text: str
    updated: str


def load_agreement_content() -> AgreementContent:
    """读取包内协议正文及版本，资源缺失或损坏时报告安装错误。"""
    try:
        data = json.loads(files("muika").joinpath("user_agreement.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Agreement content must be an object")
        title, text, updated = data["title"], data["text"], data["updated"]
        if not all(isinstance(value, str) and value.strip() for value in (title, text, updated)):
            raise ValueError("Agreement fields must be non-empty strings")
        datetime.fromisoformat(updated)
        return AgreementContent(title, text, updated)
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Bundled user agreement is missing or invalid. Reinstall Muika-After-Story.") from error


def _version_requires_update(stored: str, current: str) -> bool:
    """判断存储版本是否过期，无法比较的版本需要重新确认。"""
    try:
        return datetime.fromisoformat(stored) < datetime.fromisoformat(current)
    except (ValueError, TypeError):
        return True


@dataclass(frozen=True)
class AgreementState:
    has_agreed: bool = False
    timestamp: str = ""
    version: str = ""


@dataclass(frozen=True)
class AgreementStatus:
    content: AgreementContent
    state: AgreementState = field(default_factory=AgreementState)
    state_error: str = ""

    @property
    def needs_acceptance(self) -> bool:
        """判断当前协议是否需要确认。"""
        return not self.state.has_agreed or _version_requires_update(self.state.version, self.content.updated)


class UserAgreement:
    def __init__(self, storage_path: Path):
        self.storage_path = storage_path

    def status(self) -> AgreementStatus:
        """读取协议和同意记录，损坏记录视为未确认，读写权限错误向上传递。"""
        content = load_agreement_content()
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or type(data.get("has_agreed")) is not bool:
                raise ValueError("Agreement state must contain a boolean has_agreed")
            timestamp, version = data.get("timestamp", ""), data.get("version", "")
            if not isinstance(timestamp, str) or not isinstance(version, str):
                raise ValueError("Agreement timestamp and version must be strings")
            datetime.fromisoformat(timestamp)
            state = AgreementState(data["has_agreed"], timestamp, version)
        except FileNotFoundError:
            return AgreementStatus(content)
        except (ValueError, UnicodeError) as error:
            return AgreementStatus(content, state_error=f"Invalid agreement state: {error}")
        return AgreementStatus(content, state)

    def accept(self, version: str) -> AgreementState:
        """保存用户确认的版本，正文已更新时拒绝签署，写入失败时保留旧记录。

        :raises ValueError: 确认版本与当前正文版本不同。
        :raises OSError: 同意记录无法保存。
        """
        if version != load_agreement_content().updated:
            raise ValueError("Agreement version changed. Read and confirm the current agreement again.")
        state = AgreementState(True, datetime.now().isoformat(), version)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        stream = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.storage_path.parent, delete=False)
        temporary_path = Path(stream.name)
        try:
            with stream:
                json.dump(asdict(state), stream, indent=2, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary_path.replace(self.storage_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return state
