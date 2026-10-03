"""验证持久处理边界。

预期失败：租约被两个候选持有；重传内容变化；旧认领提交；不同客户端错投；
回复提交与处理状态不一致；服务重启复活旧租约；同一输入生成多个已提交结果。
"""

import asyncio
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from muika.database.db import close_db, init_db
from muika.node.journal import JournalConflict, RuntimeJournal
from muika.node.models import IncomingMessage, OutgoingMessage

pytestmark = pytest.mark.e2e


async def test_journal_recovers_pending_work_and_replies(tmp_path, recorder):
    """用真实数据库验证认领、提交、重启和路由。"""
    await init_db(tmp_path / "runtime.db")
    try:
        journal = RuntimeJournal()
        await journal.start()
        first = await journal.acquire("pc", 10)
        assert first is not None
        assert await journal.acquire("linux", 10) is None
        message = IncomingMessage(id="input-1", client_id="bot-a", conversation_id="room-a", text="Remember me")
        await journal.receive(message)
        await journal.receive(message)
        with pytest.raises(JournalConflict):
            await journal.receive(message.model_copy(update={"text": "Changed"}))
        claim = await journal.claim("pc", first.epoch)
        assert claim is not None and claim.message == message
        await journal.release("pc", first.epoch)
        second = await journal.acquire("linux", 10)
        assert second is not None and second.epoch > first.epoch
        recovered = await journal.claim("linux", second.epoch)
        assert recovered is not None and recovered.message == message
        reply = OutgoingMessage(id="reply-1", client_id="bot-a", conversation_id="room-a", text="I remember.")
        with pytest.raises(JournalConflict):
            await journal.commit("pc", first.epoch, claim, [reply])
        wrong_target = reply.model_copy(update={"id": "wrong-target", "client_id": "bot-b"})
        with pytest.raises(JournalConflict):
            await journal.commit("linux", second.epoch, recovered, [reply, wrong_target])
        assert await journal.pending("bot-a") == []
        assert await journal.pending("bot-b") == []
        await journal.commit("linux", second.epoch, recovered, [reply])
        await journal.commit("linux", second.epoch, recovered, [reply])
        with pytest.raises(JournalConflict):
            await journal.commit("linux", second.epoch, recovered, [reply.model_copy(update={"text": "Changed"})])
        assert await journal.claim("linux", second.epoch) is None
        assert await journal.pending("bot-b") == []
        assert await journal.pending("bot-a") == [reply]
        with pytest.raises(JournalConflict):
            await journal.acknowledge("bot-b", reply.id)
        recorder.record("checked", invariant="one_committed_reply_for_recovered_input", epoch=second.epoch)
        await close_db()
        await init_db(tmp_path / "runtime.db")
        restarted = RuntimeJournal()
        await restarted.start()
        with pytest.raises(JournalConflict):
            await restarted.renew("linux", second.epoch, 10)
        assert await restarted.pending("bot-a") == [reply]
        await restarted.acknowledge("bot-a", reply.id)
        await restarted.acknowledge("bot-a", reply.id)
        assert await restarted.pending("bot-a") == []
        recorder.record("checked", invariant="restart_keeps_outbox_and_invalidates_lease")
    finally:
        await close_db()


async def test_journal_lease_expires_without_release(tmp_path, recorder):
    """连接消失后依靠服务端期限接管，旧实例不能续租。"""
    await init_db(tmp_path / "runtime.db")
    try:
        journal = RuntimeJournal()
        await journal.start()
        grants = await asyncio.gather(journal.acquire("pc", 0.05), journal.acquire("linux", 0.05))
        assert sum(grant is not None for grant in grants) == 1
        first = next(grant for grant in grants if grant is not None)
        assert first is not None
        await asyncio.sleep(0.07)
        second = await journal.acquire("linux", 10)
        assert second is not None and second.epoch > first.epoch
        with pytest.raises(JournalConflict):
            await journal.renew(first.owner, first.epoch, 10)
        recorder.record("checked", invariant="lease_expiry_without_shutdown_hook", epoch=second.epoch)
    finally:
        await close_db()


async def test_unversioned_memory_database_upgrades_from_foreign_directory(tmp_path, recorder):
    """旧记忆库不能被误标为新版，迁移与回退保留原有数据。"""
    path = tmp_path / "old.db"
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[3] / "muika" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path.as_posix()}")
    await asyncio.to_thread(command.upgrade, config, "5e446de27cb4")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE alembic_version")
        db.execute("INSERT INTO memory_runtime (id, payload) VALUES (1, '{}')")
    await init_db(path)
    try:
        journal = RuntimeJournal()
        await journal.start()
        assert await journal.acquire("pc", 10) is not None
    finally:
        await close_db()
    await asyncio.to_thread(command.downgrade, config, "5e446de27cb4")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT payload FROM memory_runtime WHERE id=1").fetchone()[0] == "{}"
        assert db.execute("SELECT name FROM sqlite_master WHERE name='runtime_inbox'").fetchone() is None
    assert list((tmp_path / "backups").glob("*.db"))
    recorder.record("checked", invariant="legacy_upgrade_and_downgrade_keep_memory", cwd=str(Path.cwd()))
