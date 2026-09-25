"""清理 data 目录根目录散落的历史临时文件与日志，将其安全归档到 backups 目录。"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

# 核心受保护资产，禁止移动
PROTECTED_NAMES = {
    "muika.db",
    "muika.db-shm",
    "muika.db-wal",
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


if __name__ == "__main__":
    clean_data_directory()
