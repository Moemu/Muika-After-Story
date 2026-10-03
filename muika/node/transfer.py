"""导出单机快照，并核对导入常驻部署的数据和资源。"""

import hashlib
import json
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from tzlocal import get_localzone_name

from muika.database.db import close_db, init_db
from muika.node.bundle import CognitiveBundle
from muika.node.config import ServerProfile, write_private_json
from muika.node.execution_protocol import DATA_REVISION
from muika.node.service_lock import StateServiceLock

RESOURCE_TABLES = (("experience", "resources"), ("agent_task", "payload"), ("agent_call", "payload"))


def checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def inventory(db: sqlite3.Connection) -> dict[str, dict[str, str | int]]:
    """记录业务表行数和有序内容摘要，校验 schema 与数据的完整复制。"""
    result: dict[str, dict[str, str | int]] = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        quoted = '"' + table.replace('"', '""') + '"'
        rows = [json.dumps(row, ensure_ascii=False, default=str) for row in db.execute(f"SELECT * FROM {quoted}")]
        result[table] = {"rows": len(rows), "sha256": checksum("\n".join(sorted(rows)).encode())}
    return result


def rewrite_resources(db: sqlite3.Connection, convert: Callable[[str], str]) -> None:
    """只改资源结构中的路径和调用归档路径，不改记忆或工具参数文本。"""

    def visit(value: Any) -> Any:
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, dict):
            value = {key: visit(item) for key, item in value.items()}
            if "type" in value and "path" in value and value["path"]:
                value["path"] = convert(value["path"])
            if value.get("output_path"):
                value["output_path"] = convert(value["output_path"])
        return value

    for table, column in RESOURCE_TABLES:
        for id, payload in db.execute(f"SELECT id, {column} FROM {table}").fetchall():
            db.execute(f"UPDATE {table} SET {column}=? WHERE id=?", (json.dumps(visit(json.loads(payload))), id))


async def export_snapshot(source: Path, destination: Path, workspace: Path) -> dict:
    """生成独立快照，原库不升级；调用者必须先停止原单机实例。"""
    source, destination = source.resolve(), destination.resolve()
    if not source.is_file() or destination.exists():
        raise ValueError("Source database must exist and the export file must be new.")
    lock = StateServiceLock(source)
    lock.acquire()
    try:
        with tempfile.TemporaryDirectory(prefix="mas-export-") as temporary:
            root = Path(temporary)
            copied = root / "muika.db"
            with closing(sqlite3.connect(source)) as original, closing(sqlite3.connect(copied)) as snapshot:
                original.backup(snapshot)
            await init_db(copied)
            await close_db()
            files: dict[str, bytes] = {}
            for name in ("agent_tasks", "context_sources", "memory_resources"):
                directory = source.parent / name
                if directory.is_dir():
                    for path in directory.rglob("*"):
                        if path.is_file() and not path.is_symlink():
                            files[path.relative_to(source.parent).as_posix()] = path.read_bytes()

            def preserve(value: str) -> str:
                path = Path(value)
                if not path.is_file():
                    raise ValueError(f"A referenced resource is missing: {path}")
                content = path.read_bytes()
                if path.resolve().is_relative_to(source.parent) and path.resolve().relative_to(source.parent).parts[
                    0
                ] in {"agent_tasks", "context_sources", "memory_resources"}:
                    name = path.resolve().relative_to(source.parent).as_posix()
                else:
                    name = "memory_resources/" + checksum(content) + path.suffix
                files[name] = content
                return "mas-export:" + name

            with closing(sqlite3.connect(copied)) as snapshot:
                if snapshot.execute("SELECT COUNT(*) FROM runtime_authority").fetchone()[0]:
                    raise ValueError("Export supports standalone databases only; do not clone a running deployment.")
                rewrite_resources(snapshot, preserve)
                bundle = CognitiveBundle.capture(workspace)
                snapshot.execute("INSERT OR REPLACE INTO runtime_state VALUES (?, ?)", (2, bundle.model_dump_json()))
                snapshot.commit()
                report = inventory(snapshot)
                snapshot.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            files["muika.db"] = copied.read_bytes()
            manifest = {
                "data_revision": DATA_REVISION,
                "timezone": get_localzone_name(),
                "tables": report,
                "files": {name: checksum(content) for name, content in files.items()},
            }
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))
                for name, content in files.items():
                    archive.writestr(name, content)
            return manifest
    finally:
        lock.close()


def import_snapshot(source: Path, profile: ServerProfile) -> dict:
    """向空部署导入已核对的快照，原单机库仍保留用于回滚。"""
    target = profile.database.resolve()
    if target.exists():
        raise ValueError("Import requires a new deployment without a database.")
    with zipfile.ZipFile(source) as archive:
        if "manifest.json" not in archive.namelist():
            raise ValueError("Export has no manifest.")
        manifest = json.loads(archive.read("manifest.json"))
        if not isinstance(manifest, dict) or manifest.get("data_revision") != DATA_REVISION:
            raise ValueError("Export data version is incompatible.")
        try:
            if not isinstance(manifest.get("timezone"), str):
                raise ValueError("Export has no relationship timezone.")
            ZoneInfo(manifest["timezone"])
        except ZoneInfoNotFoundError as error:
            raise ValueError("Export relationship timezone is invalid.") from error
        names = manifest.get("files")
        if not isinstance(names, dict) or "muika.db" not in names or not isinstance(manifest.get("tables"), dict):
            raise ValueError("Export has no database or verification inventory.")
        if set(names) != set(archive.namelist()) - {"manifest.json"} or len(archive.namelist()) != len(names) + 1:
            raise ValueError("Export contains missing or duplicate files.")
        for name, digest in names.items():
            if not isinstance(name, str) or not isinstance(digest, str):
                raise ValueError("Export contains an invalid file entry.")
            path = Path(name)
            if not path.parts or name.endswith("/") or path.is_absolute() or ".." in path.parts or "\\" in name:
                raise ValueError("Export contains an invalid file path.")
            if name != "muika.db" and path.parts[0] not in {"agent_tasks", "context_sources", "memory_resources"}:
                raise ValueError("Export contains an unsupported file.")
            if not (target.parent / path).resolve().is_relative_to(target.parent):
                raise ValueError("Import destination contains a path outside its data directory.")
            if (target.parent / path).exists():
                raise ValueError("Import would overwrite existing deployment data.")
            if checksum(archive.read(name)) != digest:
                raise ValueError("Export file checksum does not match.")
        with tempfile.TemporaryDirectory(prefix="mas-import-", dir=profile.directory.parent) as temporary:
            staging = Path(temporary)
            for name in [item for item in names if item != "muika.db"] + ["muika.db"]:
                path = staging / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(archive.read(name))
                path.chmod(0o600)
            with closing(sqlite3.connect(staging / "muika.db")) as db, db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or inventory(db) != manifest["tables"]:
                    raise ValueError("Imported database integrity or table checksums do not match.")

                def relocate(value: str) -> str:
                    if not value.startswith("mas-export:") or value[11:] not in names:
                        raise ValueError("Imported resource has no verified file.")
                    return str(target.parent / value[11:])

                rewrite_resources(db, relocate)
            target.parent.mkdir(parents=True, exist_ok=True)
            for name in [item for item in names if item != "muika.db"] + ["muika.db"]:
                path = target if name == "muika.db" else target.parent / name
                path.parent.mkdir(parents=True, exist_ok=True)
                (staging / name).replace(path)
            write_private_json(profile.directory / "import-report.json", manifest)
            profile.timezone = manifest["timezone"]
            return manifest
