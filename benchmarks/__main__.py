"""运行入口：``uv run python -m benchmarks``。

环境变量必须在任何 ``import muika`` 之前设置——``mas_config`` 在 import 期即实例化，
``master_id`` 为空会读 ``SUPERUSERS``、``ipc_secret`` 为空会写回仓库 ``.env``。
"""

import os

os.environ.setdefault("MASTER_ID", "benchmark_master")
os.environ.setdefault("SUPERUSERS", '["benchmark_master"]')
if "IPC_SECRET" not in os.environ:
    os.environ["IPC_SECRET"] = os.urandom(32).hex()


def main() -> None:
    """CLI 入口（延迟导入，确保环境变量先就位）。"""
    from benchmarks.cli import cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
