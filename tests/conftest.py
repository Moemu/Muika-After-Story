"""共享 fixtures。

注意：环境变量必须在任何 ``import muika`` 之前设置——``mas_config`` 在 import 期
即实例化，``master_id`` 为空时会读 ``SUPERUSERS`` 环境变量、``ipc_secret`` 为空时
会把新密钥写回仓库 ``.env``。这里用 ``setdefault`` 兜底，保证 CI 无 ``.env`` 也能跑。
"""

from __future__ import annotations

import os

os.environ.setdefault("MASTER_ID", "test_master")
os.environ.setdefault("SUPERUSERS", '["test_master"]')
os.environ.setdefault("IPC_SECRET", "test-ipc-secret")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

from muika.config import mas_config  # noqa: E402
from muika.database.orm_models import Base  # noqa: E402


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """每个测试运行在独立临时 cwd 下，并把数据目录指向 tmp，防止污染真实仓库。

    注意：``mas_config`` 在 collection 期已实例化，这里只 monkeypatch 其属性。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mas_config, "data_dir", tmp_path / "data")
    yield


@pytest.fixture(autouse=True)
def reset_heartbeat():
    """每个测试结束后将 Heart 强度回归配置默认值，防止污染其他用例。

    仅当 ModelConfigManager 已被实例化（即有用例触发了它）时才复位，
    避免在尚未初始化的情况下额外拉起文件 watcher / load config。
    """
    from muika.config import _model_config_manager

    yield
    if _model_config_manager is not None:
        _model_config_manager.set_heart_intensity(mas_config.heartbeat_intensity)


@pytest_asyncio.fixture
async def db_session(tmp_path):
    """临时 SQLite + ``create_all``，供 CRUD 静态方法直接使用（绕过 Alembic 迁移）。

    与真实 ``get_session`` 一样，CRUD 不 commit，调用方需在断言前 ``commit``。
    """
    db_path = tmp_path / "test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


class FakeLLM:
    """裸 LLM stub——不继承 ``BaseLLM``，绕开 ``__init_subclass__`` 对 ``ask`` 的
    usage 写库装饰（DB 未初始化时装饰器会抛 RuntimeError）。"""

    def __init__(self, response=None, error=None, side_effect=None):
        self.response = response
        self.error = error
        self.side_effect = side_effect
        self.requests: list = []
        self.call_count = 0

    async def ask(self, request, *, stream=False):
        self.call_count += 1
        self.requests.append(request)
        if self.side_effect is not None:
            return self.side_effect(request)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def fake_llm_factory():
    """返回 ``FakeLLM`` 类，供测试按需实例化。"""
    return FakeLLM


@pytest.fixture
def session_ctx_factory():
    """构造假 ``async with get_session() as db:`` 上下文，用于把模块内延迟导入的
    ``get_session`` 绑定重定向到 ``db_session`` fixture。

    用法：``monkeypatch.setattr(module, "get_session", lambda: factory(db_session))``
    """

    class _Ctx:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            return self.session

        async def __aexit__(self, *exc):
            # 对齐真实 get_session 的成功提交语义
            await self.session.commit()
            return False

    def factory(session):
        return _Ctx(session)

    return factory


@pytest.fixture
def redirect_get_session(db_session, session_ctx_factory, monkeypatch):
    """把 ``get_session`` 相关绑定全部重定向到 ``db_session`` fixture。

    需要同时 patch 两个目标：
    - ``muika.database.db.get_session``：覆盖函数体内 ``from ... import get_session``
      的运行时重新绑定（如 ``upsert_memory``、``_get_available_candidates``）。
    - 各模块的模块级 ``get_session`` 名字：覆盖直接用模块级引用的方法
      （如 ``add_archive``、``record_topic_used``）。
    """
    factory = lambda: session_ctx_factory(db_session)  # noqa: E731
    monkeypatch.setattr("muika.database.db.get_session", factory)
    monkeypatch.setattr("muika.core.memory.get_session", factory)
    monkeypatch.setattr("muika.core.topic_manager.get_session", factory)
    return db_session
