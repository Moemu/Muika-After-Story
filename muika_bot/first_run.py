"""Bot 启动前检查协议状态。"""

from muika.config import mas_config
from muika.utils.first_run import UserAgreement


def require_user_agreement() -> None:
    """未确认当前协议时停止启动，并提示独立的确认命令。"""
    status = UserAgreement(mas_config.data_dir / "user_agreement.json").status()
    if status.needs_acceptance:
        detail = f" {status.state_error}" if status.state_error else ""
        raise RuntimeError(
            "User agreement requires acceptance. Run 'python -m muika.agreement confirm' "
            "in the instance environment, or run 'mas-launcher license', then restart the bot." + detail
        )
