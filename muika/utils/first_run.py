import json
from dataclasses import dataclass, field
from datetime import datetime
from time import sleep

from nonebot import logger
from nonebot_plugin_localstore import get_plugin_data_dir

DATA_FILE = get_plugin_data_dir() / "user_agreement.json"

AGREEMENT_TITLE = (
    "感谢你选择 Muika-After-Story（以下简称 MAS）。请仔细阅读以下条款，在你开始使用 MAS 之前，你必须同意以下许可协议："
)

AGREEMENT_TEXT = """
1. MAS 作为一款 AI 伴侣插件，可能会访问你计算机上的部分文件系统（如读取和存储用户输入、程序设置等）。所有操作将仅限于提供更加个性化的用户体验。
2. MAS 可能会记录用户输入的对话内容和其他交互信息，但所有数据仅用于改善和优化 MAS 的行为与响应，不会用于第三方数据分享和上传到任何遥测服务器，所有数据仅在本地保存。
3. MAS 会在后台运行，并可能访问互联网以获取更新，或者通过解析指定的信息源（如RSS）来提供实时内容更新。
4. MAS 是一款允许“自我行动”的 AI，她可以访问本地文件系统、浏览器、甚至进行系统级操作，如定时提醒、文件读取等。请确保在使用过程中了解并接受此类行为。
5. 你可以随时终止 MAS 的使用，并在设置中选择清除历史记录和个人数据。
"""

AGREEMENT_UPDATED = "2026-02-01"


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

    def save_agreement(self):
        """保存用户的同意状态"""
        data = {
            "has_agreed": self.agreement_state.has_agreed,
            "timestamp": self.agreement_state.timestamp.isoformat(),
            "version": self.agreement_state.version,
        }
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

        elif datetime.fromisoformat(self.agreement_state.version) < datetime.fromisoformat(AGREEMENT_UPDATED):
            logger.info("检测到协议更新")
            self.prompt_for_agreement()


user_agreement = UserAgreement()
