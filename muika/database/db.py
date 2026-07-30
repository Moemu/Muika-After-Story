"""Standalone async database engine for the Core process."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from muika.config import mas_config
from muika.utils.logger import logger

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(db_path: Path | None = None) -> None:
    """Create the engine, run pending Alembic migrations.

    :param db_path: path to the SQLite file.  Defaults to ``mas_config.data_dir / "muika.db"``.
    """
    global _engine, _session_factory

    if db_path is None:
        db_path = mas_config.data_dir / "muika.db"

    db_path.parent.mkdir(parents=True, exist_ok=True)

    # 在创建主引擎前先执行迁移，避免 SQLite 文件锁冲突
    await _run_migrations(db_path)

    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def _run_migrations(db_path: Path) -> None:
    """Run Alembic migrations programmatically.

    1. 构建指向 ``alembic.ini`` 和迁移脚本的 Alembic Config。
    2. 检测是否为旧数据库（有 ORM 表但无 ``alembic_version`` 表）
       ——若是，则 stamp HEAD 保护用户数据。
    3. 执行 ``alembic upgrade head``。
    """
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    alembic_cfg_path = Path("alembic.ini")
    alembic_cfg_path = (
        alembic_cfg_path if alembic_cfg_path.exists() else Path(__file__).resolve().parent.parent.parent / "alembic.ini"
    )
    alembic_cfg = AlembicConfig(alembic_cfg_path)
    # 运行时覆盖数据库 URL
    db_url = f"sqlite+aiosqlite:///{db_path}"
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)

    def _sync_run() -> None:
        # 自动标记旧数据库
        _ensure_alembic_version_table(db_path, alembic_cfg)
        alembic_command.upgrade(alembic_cfg, "head")

    await asyncio.to_thread(_sync_run)

    # 迁移完成后清理 alembic 模块，释放内存
    _cleanup_alembic_modules()


def _ensure_alembic_version_table(db_path: Path, alembic_cfg: AlembicConfig) -> None:
    """如果数据库文件存在且包含 ORM 表但缺少 ``alembic_version`` 表，
    直接通过 sqlite3 创建版本表并标记为 HEAD，绕过 Alembic async engine 路径以避免锁冲突。
    """
    if not db_path.exists():
        return  # 全新部署，无需标记

    from alembic.script import ScriptDirectory

    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'")
        has_alembic = cursor.fetchone() is not None
        if has_alembic:
            return  # 已有迁移历史

        # 检查是否存在 ORM 表
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('usage','memory_record','archive_record','topic_history')"
        )
        has_user_tables = cursor.fetchone() is not None

        if has_user_tables:
            logger.info("[DB] 检测到现有数据库但无 alembic_version 表" " —— 自动标记为当前版本以保护数据")
            script = ScriptDirectory.from_config(alembic_cfg)
            head = script.get_current_head()
            conn.execute(
                "CREATE TABLE IF NOT EXISTS alembic_version ("
                "    version_num VARCHAR(32) NOT NULL,"
                "    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
                ")"
            )
            conn.execute(
                "INSERT OR REPLACE INTO alembic_version (version_num) VALUES (?)",
                (head,),
            )
            conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield a scoped async session; auto-commits on success, rolls back on error.

    :raises RuntimeError: if :func:`init_db` has not been called.
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized -- call init_db() first")

    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def _cleanup_alembic_modules() -> None:
    """移除 alembic 相关模块以释放内存。"""
    import gc
    import sys

    alembic_keys = [k for k in sys.modules if k == "alembic" or k.startswith("alembic.")]
    for key in alembic_keys:
        del sys.modules[key]
    gc.collect()
    logger.debug(f"[DB] Released {len(alembic_keys)} alembic modules from memory")


async def close_db() -> None:
    """Dispose the engine (call at shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
