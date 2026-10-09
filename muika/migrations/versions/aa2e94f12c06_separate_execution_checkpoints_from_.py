"""separate execution checkpoints from memory

Revision ID: aa2e94f12c06
Revises: 7ae1ecb1f246
Create Date: 2026-10-09 20:41:20.974737

"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aa2e94f12c06"
down_revision: str | Sequence[str] | None = "7ae1ecb1f246"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """导出执行现场后移除数据库副本。"""
    connection = op.get_bind()
    directory = Path(connection.engine.url.database or ".").resolve().parent / "agent_tasks"
    tables = set(sa.inspect(connection).get_table_names())
    tasks = (
        list(connection.execute(sa.text("SELECT id, payload FROM agent_task")).mappings())
        if "agent_task" in tables
        else []
    )
    for row in tasks:
        task = json.loads(row["payload"])
        calls = [
            json.loads(item[0])
            for item in connection.execute(
                sa.text("SELECT payload FROM agent_call WHERE task_id=:id"), {"id": row["id"]}
            )
        ]
        target = directory / row["id"] / "checkpoint.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if (
            target.exists()
            and json.loads(target.read_text(encoding="utf-8"))["task"]["updated_at"] > task["updated_at"]
        ):
            continue
        temporary = target.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as file:
            json.dump({"task": task, "calls": calls}, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(target)
    events = list(connection.execute(sa.text("SELECT sequence, payload FROM sync_event")).mappings())
    if events:
        archive = directory / "sync-before-aa2e94f12c06.json"
        if not archive.exists():
            archive.parent.mkdir(parents=True, exist_ok=True)
            temporary = archive.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump([dict(row) for row in events], file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(archive)
        for row in events:
            activity = json.loads(row["payload"])
            activity.pop("tasks", None)
            activity.pop("calls", None)
            connection.execute(
                sa.text("UPDATE sync_event SET payload=:payload WHERE sequence=:sequence"),
                {"payload": json.dumps(activity, ensure_ascii=False), "sequence": row["sequence"]},
            )
    removed = list(connection.execute(sa.text("SELECT * FROM experience WHERE source LIKE 'task_call:%'")).mappings())
    if removed:
        archive = directory / "memory-before-aa2e94f12c06.json"
        if not archive.exists():
            directory.mkdir(parents=True, exist_ok=True)
            original = {
                table: [dict(row) for row in connection.execute(sa.text(f"SELECT * FROM {table}")).mappings()]
                for table in ("experience", "fact", "diary", "memory_runtime", "sync_reference")
            }
            temporary = archive.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(original, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(archive)
        removed_refs = {f"experience:{row['id']}" for row in removed}
        sources = list(
            connection.execute(
                sa.text("SELECT source, local_id FROM sync_reference WHERE source LIKE '%:experience:%'")
            ).mappings()
        )
        removed_ids = {row["id"] for row in removed}
        removed_sources = {row["source"] for row in sources if row["local_id"] in removed_ids}
        retained_sources = [row["source"] for row in sources if row["local_id"] not in removed_ids]
        retained_ids = [
            row[0]
            for row in connection.execute(
                sa.text("SELECT id FROM experience WHERE source IS NULL OR source NOT LIKE 'task_call:%' ORDER BY id")
            )
        ]
        for diary in connection.execute(sa.text("SELECT id, covered_through FROM diary")).mappings():
            if diary["covered_through"] in removed_ids:
                through = max((value for value in retained_ids if value <= diary["covered_through"]), default=0)
                connection.execute(
                    sa.text("UPDATE diary SET covered_through=:through WHERE id=:id"),
                    {"id": diary["id"], "through": through},
                )
        for table in ("fact", "diary"):
            for row in connection.execute(sa.text(f"SELECT id, source_refs FROM {table}")).mappings():
                refs = json.loads(row["source_refs"])
                if removed_refs.intersection(refs):
                    values = {"id": row["id"], "refs": json.dumps([ref for ref in refs if ref not in removed_refs])}
                    retired = ", active=0" if table == "fact" else ""
                    connection.execute(sa.text(f"UPDATE {table} SET source_refs=:refs{retired} WHERE id=:id"), values)
        for row in connection.execute(sa.text("SELECT id, payload FROM memory_runtime")).mappings():
            snapshot = json.loads(row["payload"])
            snapshot["working_summary"], snapshot["summary_through"] = "", 0
            for intention in snapshot.get("state", {}).get("intentions", []):
                intention["source_refs"] = [ref for ref in intention.get("source_refs", []) if ref not in removed_refs]
            connection.execute(
                sa.text("UPDATE memory_runtime SET payload=:payload WHERE id=:id"),
                {"id": row["id"], "payload": json.dumps(snapshot, ensure_ascii=False)},
            )
        # Gateway.start 独立迁移 gateway.db；两处须保持相同的经历删除和日记水位规则。
        for row in events:
            activity = json.loads(row["payload"])
            activity.pop("tasks", None)
            activity.pop("calls", None)
            obsolete = {
                f"experience:{item['id']}"
                for item in activity.get("experiences", [])
                if (item.get("source") or "").startswith("task_call:")
            }
            obsolete.update(ref for ref, source in activity.get("references", {}).items() if source in removed_sources)
            activity["experiences"] = [
                item for item in activity.get("experiences", []) if f"experience:{item['id']}" not in obsolete
            ]
            for fact in activity.get("facts", []):
                if obsolete.intersection(fact.get("source_refs", [])):
                    fact["active"] = False
                    fact["source_refs"] = [ref for ref in fact.get("source_refs", []) if ref not in obsolete]
            for diary in activity.get("diaries", []):
                diary["source_refs"] = [ref for ref in diary.get("source_refs", []) if ref not in obsolete]
                watermark = f"experience:{diary['covered_through']}"
                if watermark in obsolete:
                    source = activity["references"][watermark]
                    origin, _, value = source.rpartition(":experience:")
                    candidates = [
                        (int(item.rpartition(":experience:")[2]), item)
                        for item in retained_sources
                        if item.rpartition(":experience:")[0] == origin
                        and int(item.rpartition(":experience:")[2]) <= int(value)
                    ]
                    through, replacement = max(candidates, default=(0, ""))
                    if through:
                        keys = {
                            int(ref.split(":")[1]) for ref in activity["references"] if ref.startswith("experience:")
                        }
                        existing = next(
                            (
                                int(ref.split(":")[1])
                                for ref, source in activity["references"].items()
                                if ref.startswith("experience:") and source == replacement
                            ),
                            None,
                        )
                        through = existing if existing is not None else max(keys, default=0) + 1
                        activity["references"][f"experience:{through}"] = replacement
                    diary["covered_through"] = through
            activity["references"] = {
                ref: source for ref, source in activity.get("references", {}).items() if ref not in obsolete
            }
            snapshot = activity.get("snapshot")
            if snapshot:
                snapshot["working_summary"], snapshot["summary_through"] = "", 0
                for intention in snapshot.get("state", {}).get("intentions", []):
                    intention["source_refs"] = [ref for ref in intention.get("source_refs", []) if ref not in obsolete]
            connection.execute(
                sa.text("UPDATE sync_event SET payload=:payload WHERE sequence=:sequence"),
                {"payload": json.dumps(activity, ensure_ascii=False), "sequence": row["sequence"]},
            )
        connection.execute(sa.text("DELETE FROM experience WHERE source LIKE 'task_call:%'"))
        for row in removed:
            connection.execute(
                sa.text("DELETE FROM sync_reference WHERE local_id=:id AND source LIKE '%:experience:%'"),
                {"id": row["id"]},
            )
    for table in ("agent_call", "agent_task"):
        if table in tables:
            op.drop_table(table)


def downgrade() -> None:
    """从文件检查点恢复旧版执行表，文件继续保留。"""
    op.create_table(
        "agent_task",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_agent_task_status", "agent_task", ["status"])
    op.create_index("ix_agent_task_created_at", "agent_task", ["created_at"])
    op.create_table(
        "agent_call",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_agent_call_task_id", "agent_call", ["task_id"])
    op.create_index("ix_agent_call_status", "agent_call", ["status"])
    connection = op.get_bind()
    directory = Path(connection.engine.url.database or ".").resolve().parent / "agent_tasks"
    for path in directory.glob("*/checkpoint.json"):
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
        task = checkpoint["task"]
        connection.execute(
            sa.text("INSERT INTO agent_task VALUES (:id, :status, :revision, :created_at, :updated_at, :payload)"),
            {
                **{key: task[key] for key in ("id", "status", "revision", "created_at", "updated_at")},
                "payload": json.dumps(task, ensure_ascii=False),
            },
        )
        for call in checkpoint["calls"]:
            connection.execute(
                sa.text("INSERT INTO agent_call VALUES (:id, :task_id, :status, :payload)"),
                {
                    **{key: call[key] for key in ("id", "task_id", "status")},
                    "payload": json.dumps(call, ensure_ascii=False),
                },
            )
