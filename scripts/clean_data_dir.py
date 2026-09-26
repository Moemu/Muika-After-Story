"""清理 data 目录根目录散落的历史临时文件与日志，将其安全归档到 backups 目录。"""

from __future__ import annotations

import argparse
import shutil
from datetime import datetime
from pathlib import Path

# 白名单是编写时的运行时快照：核心后续若在 data 根新增合法子目录，
# 必须同步补充此处，否则再次运行本脚本时会被归档。仅针对一次性历史清理。
PROTECTED_NAMES = {
    "muika.db",
    "muika.db-shm",
    "muika.db-wal",
    "muika.db-journal",
    "muika.dev.db",
    "user_agreement.json",
    "restart.json",
    # 合法子目录
    "agent_tasks",
    "agent_processes",
    "connection_records",
    "context_sources",
    "memory_resources",
    "self_modifications",
    "downloads",
    "backups",
    "tmp",
    "plugin",
    "reviews",
    "core_proposals",
}


def clean_data_directory(data_dir: Path | None = None, dry_run: bool = False) -> list[Path]:
    """扫描并归档 data 目录根下的杂散文件。

    :param data_dir: 数据目录路径，默认为 Path("data")。
    :param dry_run: 若为 True 仅报告不执行移动。
    :return: 被归档的文件或目录列表。
    """
    if data_dir is None:
        data_dir = Path("data").resolve()

    if not data_dir.exists():
        print(f"Data directory {data_dir} does not exist.")
        return []

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = data_dir / "backups" / f"legacy_data_cleanup_{timestamp}"

    to_archive: list[Path] = []
    for item in data_dir.iterdir():
        if item.name in PROTECTED_NAMES:
            continue
        to_archive.append(item)

    if not to_archive:
        print("Data directory is clean. No stray files found.")
        return []

    print(f"Found {len(to_archive)} stray item(s) in {data_dir}.")
    if dry_run:
        for item in to_archive:
            print(f"  [Dry-run] Would archive: {item.name}")
        return to_archive

    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[Path] = []
    for item in to_archive:
        target = archive_dir / item.name
        try:
            shutil.move(str(item), str(target))
            archived.append(target)
            print(f"  Archived: {item.name} -> {target.relative_to(data_dir)}")
        except Exception as exc:
            print(f"  Failed to archive {item.name}: {exc}")

    print(f"Successfully archived {len(archived)} items to {archive_dir}.")
    return archived


def main() -> int:
    """运行命令行清理。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", nargs="?", default="data", help="数据目录路径，默认 ./data")
    parser.add_argument("--dry-run", action="store_true", help="仅报告将归档的条目，不执行移动")
    args = parser.parse_args()
    clean_data_directory(Path(args.data_dir).resolve(), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
