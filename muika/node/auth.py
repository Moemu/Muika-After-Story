"""持久保存独立节点凭据和一次性配对邀请。"""

import hashlib
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class NodeCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    role: Literal["bot", "core", "executor"]
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    priority: int = 100


class CredentialStore:
    """用本机短事务消费邀请，撤销后现有连接也失去授权。"""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS node (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS invitation "
                "(digest TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL)"
            )
        path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            yield db

    def credentials(self) -> list[NodeCredential]:
        with self.connect() as db:
            return [NodeCredential.model_validate_json(row[0]) for row in db.execute("SELECT payload FROM node")]

    def issue(self, id: str, role: Literal["bot", "core", "executor"], priority: int = 100) -> str:
        """创建十分钟有效的邀请，设备名和角色由服务端固定。"""
        code = secrets.token_urlsafe(24)
        credential = NodeCredential(id=id, role=role, token_sha256="0" * 64, priority=priority)
        with self.connect() as db:
            if db.execute("SELECT id FROM node WHERE id=?", (id,)).fetchone():
                raise ValueError("Node is already paired. Revoke it before pairing again.")
            db.execute(
                "INSERT INTO invitation VALUES (?, ?, ?)",
                (hashlib.sha256(code.encode()).hexdigest(), credential.model_dump_json(), time.time() + 600),
            )
        return code

    def redeem(self, code: str) -> tuple[NodeCredential, str]:
        """一次性换取独立长期 token，凭据库只保存其摘要。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256(code.encode()).hexdigest()
            row = db.execute("SELECT payload, expires FROM invitation WHERE digest=?", (digest,)).fetchone()
            if row is None or row[1] < time.time():
                raise ValueError("Pairing code is invalid, expired or already used.")
            credential = NodeCredential.model_validate_json(row[0])
            token = secrets.token_urlsafe(32)
            credential.token_sha256 = hashlib.sha256(token.encode()).hexdigest()
            db.execute("INSERT INTO node VALUES (?, ?)", (credential.id, credential.model_dump_json()))
            db.execute("DELETE FROM invitation WHERE digest=?", (digest,))
        return credential, token

    def revoke(self, id: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM node WHERE id=?", (id,))
            for digest, payload in db.execute("SELECT digest, payload FROM invitation").fetchall():
                if NodeCredential.model_validate_json(payload).id == id:
                    db.execute("DELETE FROM invitation WHERE digest=?", (digest,))
