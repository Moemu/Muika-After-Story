import json
from dataclasses import dataclass, field
from datetime import datetime
from importlib.resources import files
from time import sleep

from muika.config import mas_config
from muika.utils.logger import logger

DATA_FILE = mas_config.data_dir / "user_agreement.json"


def _load_agreement_content() -> tuple[str, str, str]:
    """读取包内协议正文及版本，资源缺失或损坏时报告安装错误。

    :raises RuntimeError: 包内协议资源无法读取或字段无效。
    """
    try:
        data = json.loads(files("muika").joinpath("user_agreement.json").read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Agreement content must be an object")
        title, text, updated = data["title"], data["text"], data["updated"]
        if not all(isinstance(value, str) and value.strip() for value in (title, text, updated)):
            raise ValueError("Agreement fields must be non-empty strings")
        datetime.fromisoformat(updated)
        return title, text, updated
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        raise RuntimeError("Bundled user agreement is missing or invalid. Reinstall Muika-After-Story.") from error


AGREEMENT_TITLE, AGREEMENT_TEXT, AGREEMENT_UPDATED = _load_agreement_content()


def _version_requires_update(stored: str, current: str) -> bool:
    """存储的协议版本是否落后于当前版本（空值或无法解析视为需要重新签署）"""
    if not stored or not current:
        return True
    try:
        return datetime.fromisoformat(stored) < datetime.fromisoformat(current)
    except ValueError:
        return True


@dataclass
class AgreementState:
    has_agreed: bool = False
    timestamp: datetime = field(default_factory=datetime.now)
    version: str = AGREEMENT_UPDATED


class UserAgreement:
    def __init__(self):
        self.agreement_state = AgreementState()
        self.storage_path = DATA_FILE

    def load_agreement(self):
        """加载用户的同意状态"""
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.agreement_state.has_agreed = data.get("has_agreed", False)
                self.agreement_state.timestamp = datetime.fromisoformat(data.get("timestamp", ""))
                self.agreement_state.version = data.get("version", "")
        except Exception as e:
            logger.error(f"加载用户协议失败: {e}，重新签署协议...")

    def save_agreement(self) -> None:
        """保存用户的同意状态"""
        data = {
            "has_agreed": self.agreement_state.has_agreed,
            "timestamp": self.agreement_state.timestamp.isoformat(),
            "version": self.agreement_state.version,
        }
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def prompt_for_agreement(self):
        """展示协议并等待用户同意"""
        print(AGREEMENT_TITLE)
        sleep(1)
        print(AGREEMENT_TEXT)
        sleep(5)
        print(f"以上条款更新于: {AGREEMENT_UPDATED}。您必须同意以上条款和阅读许可证声明后才可继续使用 MAS")

        user_input = input("同意吗？(是/否): ")

        if user_input.lower() in ["是", "y"]:
            self.agreement_state.has_agreed = True
            self.agreement_state.timestamp = datetime.now()
            self.agreement_state.version = AGREEMENT_UPDATED
            self.save_agreement()
            print("感谢您的同意，MAS 将开始运行")
        else:
            print("您未同意协议，MAS 无法继续运行。")
            exit(0)

    def check_first_run(self):
        self.load_agreement()

        if not self.agreement_state.has_agreed:
            self.prompt_for_agreement()

        elif _version_requires_update(self.agreement_state.version, AGREEMENT_UPDATED):
            logger.info("检测到协议更新")
            self.prompt_for_agreement()


user_agreement = UserAgreement()
