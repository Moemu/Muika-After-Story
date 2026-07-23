"""Standalone async database engine for the Core process."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from muika.config import mas_config
from muika.database.orm_models import Base

_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(db_path: Path | None = None) -> None:
    """Create the engine, ensure tables exist.

    :param db_path: path to the SQLite file.  Defaults to ``mas_config.data_dir / "muika.db"``.
    """
    global _engine, _session_factory

    if db_path is None:
        db_path = mas_config.data_dir / "muika.db"

    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path}"
    _engine = create_async_engine(url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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


async def close_db() -> None:
    """Dispose the engine (call at shutdown)."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
