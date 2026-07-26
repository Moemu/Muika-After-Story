"""Alembic async environment for Muika-After-Story database migrations.

在 ``init_db()`` 中被自动调用，在应用启动时执行数据库迁移。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from muika.config import mas_config
from muika.database.orm_models import Base

# Alembic Config 对象，由 alembic.command 传入
config = context.config

# 应用 alembic.ini 中的日志配置
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 运行时覆盖数据库 URL（来自应用配置，非 alembic.ini 的占位值）
db_path = mas_config.data_dir / "muika.db"
config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")

# autogenerate 对比的目标元数据
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """以 "离线" 模式运行迁移 —— 生成 SQL 而非直接连接数据库。

    需要 ``context.configure()`` 提供 ``url`` 参数（从配置中获取）。
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """在给定的同步连接上配置并执行迁移。"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite ALTER 支持
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """创建异步引擎并在线执行迁移。"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """以 "在线" 模式运行迁移 —— 连接数据库并执行。"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
