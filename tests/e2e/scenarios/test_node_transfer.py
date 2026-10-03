"""验证从单机库导入新部署，保留人格、资源和回滚原库。

失败边界：原库被升级；附件仍指向旧设备；导入被篡改；覆盖已有部署；
不完整归档被当作成功迁移；配置与行数摘要缺失。
"""

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import select

from muika.config import mas_config
from muika.core.memory import MemoryManager
from muika.database.db import close_db, get_session, init_db
from muika.database.orm_models import ExperienceORM
from muika.models import Resource
from muika.node.config import ServerProfile
from muika.node.transfer import export_snapshot, import_snapshot

pytestmark = pytest.mark.e2e


async def test_export_import_verifies_identity_resources_and_original_database(monkeypatch, tmp_path, recorder):
    original = tmp_path / "original"
    original.mkdir()
    monkeypatch.setattr(mas_config, "data_dir", original)
    attachment = original / "poem.txt"
    attachment.write_text("Our poem", encoding="utf-8")
    database = original / "muika.db"
    await init_db(database)
    memory = MemoryManager()
    await memory.load()
    session = memory.session.session_id
    await memory.add_context("user", "Remember our poem.", resources=[Resource(type="file", path=str(attachment))])
    await close_db()
    original_bytes = database.read_bytes()
    workspace = tmp_path / "workspace"
    (workspace / "configs").mkdir(parents=True)
    (workspace / "configs/models.yml").write_text("main:\n  provider: _echo\n  default: true\n", encoding="utf-8")
    archive = tmp_path / "snapshot.zip"
    manifest = await export_snapshot(database, archive, workspace)
    assert database.read_bytes() == original_bytes
    assert manifest["tables"]["experience"]["rows"] == 1
    target = tmp_path / "server"
    target.mkdir()
    profile = ServerProfile(
        directory=target, database=target / "muika.db", public_address="ws://127.0.0.1:8766/node/ws"
    )
    report = import_snapshot(archive, profile)
    assert report == json.loads((target / "import-report.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError, match="new deployment"):
        import_snapshot(archive, profile)
    monkeypatch.setattr(mas_config, "data_dir", target)
    await init_db(profile.database)
    try:
        restored = MemoryManager()
        await restored.load()
        assert restored.session.session_id == session
        assert restored.recent_turns[0].content == "Remember our poem."
        async with get_session() as db:
            row = await db.scalar(select(ExperienceORM))
            resource = Resource(**json.loads(row.resources)[0])
        assert resource.path.startswith(str(target))
        assert resource.path != str(attachment)
        assert Path(resource.path).read_bytes() == b"Our poem"
    finally:
        await close_db()
    damaged = tmp_path / "damaged.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(damaged, "w") as destination:
        for name in source.namelist():
            destination.writestr(name, b"changed" if name == "muika.db" else source.read(name))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="checksum"):
        import_snapshot(damaged, profile.model_copy(update={"directory": empty, "database": empty / "muika.db"}))
    assert not (empty / "muika.db").exists()
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM experience").fetchone()[0] == 1
    recorder.record("checked", invariant="verified_import_preserves_identity_resources_and_original_rollback_database")
