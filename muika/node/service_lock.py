"""防止同一数据库被多个状态服务同时授予控制权。"""

import sqlite3
from pathlib import Path


class StateServiceLock:
    """使用独立 SQLite 锁文件持有进程级互斥，进程退出后由系统释放。"""

    def __init__(self, database: Path) -> None:
        self.path = database.with_name(database.name + ".service-lock")
        self._connection: sqlite3.Connection | None = None

    def acquire(self) -> None:
        """立即获取服务锁，已被占用时报告冲突。"""
        if self._connection is not None:
            raise RuntimeError("State service lock is already held.")
        connection = sqlite3.connect(self.path, timeout=0, isolation_level=None)
        try:
            connection.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            connection.close()
            raise RuntimeError("Another state service already owns this database.") from exc
        self._connection = connection

    def close(self) -> None:
        """释放服务锁，不删除锁文件。"""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
