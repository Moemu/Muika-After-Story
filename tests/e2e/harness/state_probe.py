"""P0 状态服务实验：验证出站连接、短事务与任期隔离，不接管生产 Core。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import time
from pathlib import Path

from aiohttp import web


class ProbeStore:
    """保存实验输入、回复和检查点，所有写入经过同一个短事务。"""

    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            "CREATE TABLE IF NOT EXISTS authority (id INTEGER PRIMARY KEY, epoch INTEGER, owner TEXT, deadline REAL);"
            "INSERT OR IGNORE INTO authority VALUES (1, 0, '', 0);"
            "CREATE TABLE IF NOT EXISTS inbox ("
            "id TEXT PRIMARY KEY, client TEXT, body TEXT, epoch INTEGER, processed INTEGER DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS outbox ("
            "id TEXT PRIMARY KEY, client TEXT, body TEXT, delivered INTEGER DEFAULT 0);"
            "CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, body TEXT);"
            "UPDATE authority SET epoch=epoch+1, owner='', deadline=0 WHERE id=1;"
        )
        self.lock = asyncio.Lock()

    def transact(self, client: str, role: str, payload: dict) -> dict:
        """模拟业务事务，并拒绝失效任期与跨客户端确认。"""
        db = self.connection
        db.execute("BEGIN IMMEDIATE")
        try:
            result = self._apply(client, role, payload)
            db.execute("COMMIT")
            return result
        except (KeyError, ValueError) as exc:
            db.execute("ROLLBACK")
            return {"error": str(exc)}
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def _apply(self, client: str, role: str, payload: dict) -> dict:
        db = self.connection
        operation = payload["op"]
        epoch, owner, deadline = db.execute("SELECT epoch, owner, deadline FROM authority WHERE id=1").fetchone()
        now = time.monotonic()
        if operation == "acquire" and role == "core":
            if owner and now < deadline:
                raise ValueError("lease_busy")
            epoch += 1
            db.execute("UPDATE authority SET epoch=?, owner=?, deadline=? WHERE id=1", (epoch, client, now + 1.0))
            return {"epoch": epoch}
        if operation in {"renew", "claim", "commit"}:
            if role != "core" or owner != client or epoch != payload["epoch"] or now >= deadline:
                raise ValueError("stale_owner")
            if operation == "renew":
                db.execute("UPDATE authority SET deadline=? WHERE id=1", (now + 1.0,))
                return {"epoch": epoch}
            if operation == "claim":
                row = db.execute(
                    "SELECT id, client, body FROM inbox WHERE processed=0 ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row is None:
                    return {"input": None}
                db.execute("UPDATE inbox SET epoch=? WHERE id=?", (epoch, row[0]))
                return {"input": {"id": row[0], "client": row[1], "body": row[2]}}
            row = db.execute("SELECT client, epoch, processed FROM inbox WHERE id=?", (payload["id"],)).fetchone()
            if row is None or row[1] != epoch:
                raise ValueError("not_claimed")
            if not row[2]:
                db.execute(
                    "INSERT INTO outbox(id, client, body) VALUES (?, ?, ?)", (payload["id"], row[0], payload["reply"])
                )
                db.execute("INSERT OR REPLACE INTO checkpoint VALUES (1, ?)", (payload["checkpoint"],))
                db.execute("UPDATE inbox SET processed=1 WHERE id=?", (payload["id"],))
            return {"processed": payload["id"]}
        if role != "bot":
            raise ValueError("forbidden")
        if operation == "receive":
            existing = db.execute("SELECT client, body FROM inbox WHERE id=?", (payload["id"],)).fetchone()
            if existing is not None and existing != (client, payload["body"]):
                raise ValueError("input_conflict")
            db.execute(
                "INSERT OR IGNORE INTO inbox(id, client, body) VALUES (?, ?, ?)",
                (payload["id"], client, payload["body"]),
            )
            return {"received": payload["id"]}
        if operation == "pending":
            return {
                "messages": [
                    dict(zip(("id", "body"), row))
                    for row in db.execute(
                        "SELECT id, body FROM outbox WHERE client=? AND delivered=0 ORDER BY rowid", (client,)
                    )
                ]
            }
        if operation == "delivered":
            db.execute("UPDATE outbox SET delivered=1 WHERE id=? AND client=?", (payload["id"], client))
            return {"acknowledged": payload["id"]}
        raise ValueError("unsupported_operation")


async def serve(directory: Path, ready: Path, token: str) -> None:
    """启动独立实验进程，在退出时关闭连接与数据库。"""
    directory.mkdir(parents=True, exist_ok=True)
    store = ProbeStore(directory / "probe.sqlite")
    connections: set[web.WebSocketResponse] = set()

    async def handler(request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("Authorization") != f"Bearer {token}":
            raise web.HTTPUnauthorized()
        client, role = request.query.get("client", ""), request.query.get("role", "")
        if not client or role not in {"bot", "core"}:
            raise web.HTTPBadRequest()
        ws = web.WebSocketResponse(max_msg_size=2 * 1024 * 1024)
        await ws.prepare(request)
        connections.add(ws)
        try:
            async for frame in ws:
                if frame.type != web.WSMsgType.TEXT:
                    break
                started = time.perf_counter()
                async with store.lock:
                    result = await asyncio.to_thread(store.transact, client, role, json.loads(frame.data))
                await ws.send_json({**result, "server_ms": (time.perf_counter() - started) * 1000})
        finally:
            connections.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/ws", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    ready.write_text(json.dumps({"port": runner.addresses[0][1]}), encoding="utf-8")
    try:
        await asyncio.Event().wait()
    finally:
        for ws in connections.copy():
            await ws.close()
        await runner.cleanup()
        store.connection.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.directory, args.ready, args.token))
