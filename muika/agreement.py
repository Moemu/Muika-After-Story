"""终端协议确认入口及启动器使用的 JSON 接口。"""

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from muika.utils.first_run import UserAgreement, load_agreement_content


class AgreementSettings(BaseSettings):
    data_dir: Path = Path("./data")
    model_config = SettingsConfigDict(extra="ignore", env_file=".env")


def main(argv: list[str] | None = None) -> int:
    """执行正文查询、状态查询或显式确认，失败时返回非零退出码。"""
    parser = argparse.ArgumentParser(description="Read and confirm the Muika user agreement")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="Print agreement content as JSON")
    commands.add_parser("status", help="Print agreement status as JSON")
    commands.add_parser("confirm", help="Read and confirm the agreement interactively")
    accept = commands.add_parser("accept", help="Save explicit acceptance of the displayed version")
    accept.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "show":
            print(json.dumps(asdict(load_agreement_content())))
            return 0
        agreement = UserAgreement(AgreementSettings().data_dir / "user_agreement.json")
        if args.command == "accept":
            print(json.dumps(asdict(agreement.accept(args.version))))
            return 0
        status = agreement.status()
        if args.command == "status":
            print(json.dumps({**asdict(status), "needs_acceptance": status.needs_acceptance}))
            return 0
        if status.state_error:
            print(status.state_error, file=sys.stderr)
        if not status.needs_acceptance:
            print(f"协议已确认（版本 {status.state.version}）。")
            return 0
        print(status.content.title)
        print(status.content.text)
        print(f"以上条款更新于 {status.content.updated}。请阅读协议和许可证声明后确认。")
        try:
            answer = input("同意吗？(是/否): ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"是", "y", "yes"}:
            print("未同意协议，MAS 无法继续运行。", file=sys.stderr)
            return 1
        agreement.accept(status.content.updated)
        print("协议已确认，可以启动 MAS。")
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Agreement error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
