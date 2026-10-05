"""常驻聊天入口：保存真实活动，协调在线 Core，不运行人格或工具。"""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
from aiohttp import WSMsgType, web
from pydantic import TypeAdapter

from muika.config import mas_config
from muika.models import AdapterInfo
from muika.utils.logger import logger

from .attachments import attachment_routes
from .protocol import BotToCoreEvent, BotToCoreMessage, CoreToBotMessage
from .server import CoreWsServer
from .sync_models import MAX_SYNC_BYTES, SYNC_PROTOCOL, Activity, SyncEntry

CORE_TIMEOUT = 15.0


@dataclass
class CoreConnection:
    """已认证 Core 的连接和最近活动，不包含远程执行能力。"""

    name: str
    origin: str
    priority: int
    ws: web.WebSocketResponse
    last_seen: float
    ready: bool = False


class Gateway:
    """复用 Bot IPC，将在线输出与历史同步分开。"""

    def __init__(self, directory: Path, host: str, port: int, secret: str) -> None:
        self.directory, self.secret = directory, secret
        self.bots = CoreWsServer(host, port, secret)
        self.bots.add_routes([web.get("/cores", self._connect), *attachment_routes(directory / "attachments", secret)])
        for kind in ("user_message", "command", "session_bootstrap", "session_end"):
            self.bots.register_handler(kind, self._input)
        self.nodes: dict[str, CoreConnection] = {}
        self.active: str | None = None
        self.epoch = 0
        self._lock = asyncio.Lock()
        self._clock: asyncio.Task[None] | None = None
        self.db: aiosqlite.Connection

    async def start(self) -> None:
        """恢复活动日志后开放聊天和 Core 连接。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.directory / "gateway.db")
        await self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS activity(sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS incoming(adapter TEXT, id TEXT, payload TEXT NOT NULL,
                pending INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(adapter, id));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value INTEGER NOT NULL);
        """)
        async with self.db.execute("SELECT value FROM settings WHERE key='epoch'") as cursor:
            saved = await cursor.fetchone()
        self.epoch = int(saved[0]) + 1 if saved else 1
        await self._save_epoch()
        await self.bots.start()
        self._clock = asyncio.create_task(self._watch())
        logger.success("[Gateway] Chat and Core connections are ready.")

    async def stop(self) -> None:
        """关闭连接和本地日志，不依赖退出通知交接。"""
        if self._clock is not None:
            self._clock.cancel()
            await asyncio.gather(self._clock, return_exceptions=True)
        for node in list(self.nodes.values()):
            await node.ws.close()
        await self.bots.stop()
        await self.db.close()

    async def _save_epoch(self) -> None:
        await self.db.execute("INSERT OR REPLACE INTO settings VALUES ('epoch', ?)", (self.epoch,))
        await self.db.commit()

    async def _history(self, ws: web.WebSocketResponse, after: int) -> None:
        async with self.db.execute(
            "SELECT sequence, payload FROM activity WHERE sequence > ? ORDER BY sequence LIMIT 64", (after,)
        ) as cursor:
            rows = await cursor.fetchall()
        complete = len(rows) < 64
        size = 0
        for index, row in enumerate(rows):
            size += len(row[1].encode())
            if index and size > MAX_SYNC_BYTES // 2:
                rows, complete = rows[:index], False
                break
        entries = [SyncEntry(gateway_sequence=row[0], activity=Activity.model_validate_json(row[1])) for row in rows]
        await ws.send_json(
            {
                "kind": "history",
                "entries": [entry.model_dump(mode="json") for entry in entries],
                "complete": complete,
                "through": rows[-1][0] if rows else after,
            }
        )

    async def _elect(self, preferred: str | None = None) -> None:
        available = [
            node
            for node in self.nodes.values()
            if node.ready and not node.ws.closed and time.monotonic() - node.last_seen < CORE_TIMEOUT
        ]
        names = {node.name for node in available}
        chosen = (
            preferred
            if preferred in names
            else (
                self.active
                if self.active in names
                else (max(available, key=lambda node: (node.priority, node.name)).name if available else None)
            )
        )
        if chosen != self.active:
            self.active, self.epoch = chosen, self.epoch + 1
            await self._save_epoch()
            logger.info(f"[Gateway] Active Core: {self.active}; epoch: {self.epoch}.")
        for node in self.nodes.values():
            if not node.ws.closed:
                await node.ws.send_json(
                    {"kind": "role", "active": self.active, "epoch": self.epoch, "nodes": sorted(names)}
                )
        if self.active is not None:
            async with self.db.execute("SELECT adapter,payload FROM incoming WHERE pending=1 ORDER BY rowid") as cursor:
                for adapter, payload in await cursor.fetchall():
                    await self.nodes[self.active].ws.send_json(
                        {"kind": "input", "adapter": adapter, "message": json.loads(payload), "epoch": self.epoch}
                    )

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(1)
            async with self._lock:
                if self.active and time.monotonic() - self.nodes[self.active].last_seen >= CORE_TIMEOUT:
                    await self._elect()

    async def _input(self, message: dict, adapter: AdapterInfo) -> None:
        event: BotToCoreEvent = TypeAdapter(BotToCoreMessage).validate_python(message)
        async with self._lock:
            result = await self.db.execute(
                "INSERT OR IGNORE INTO incoming(adapter,id,payload) VALUES(?,?,?)",
                (adapter.client_name, event.id, event.model_dump_json()),
            )
            await self.db.commit()
            if self.active is not None and result.rowcount:
                self.bots.set_triggering_adapter(adapter.client_name)
                await self.nodes[self.active].ws.send_json(
                    {
                        "kind": "input",
                        "adapter": adapter.client_name,
                        "message": event.model_dump(mode="json"),
                        "epoch": self.epoch,
                    }
                )

    async def _packet(self, node: CoreConnection, message: dict) -> None:
        kind = message["kind"]
        if kind == "history":
            await self._history(node.ws, int(message["after"]))
        elif kind == "heartbeat":
            if self.active is None and node.ready:
                await self._elect()
        elif kind == "ready":
            node.ready = True
            previous = self.nodes.get(self.active) if self.active else None
            prefer = message.get("foreground") or (previous is not None and node.priority > previous.priority)
            await self._elect(node.name if prefer else None)
        elif kind == "publish":
            activities = TypeAdapter(list[Activity]).validate_python(message["activities"])
            entries: list[SyncEntry] = []
            for activity in activities:
                if activity.origin != node.origin:
                    raise ValueError("Activity origin does not match this Core")
                await self.db.execute(
                    "INSERT OR IGNORE INTO activity(id,payload) VALUES(?,?)", (activity.id, activity.model_dump_json())
                )
                async with self.db.execute("SELECT sequence FROM activity WHERE id=?", (activity.id,)) as cursor:
                    position = await cursor.fetchone()
                    if position is not None:
                        entries.append(SyncEntry(gateway_sequence=position[0], activity=activity))
                for experience in activity.experiences:
                    if experience.source and experience.source.startswith("ipc:"):
                        adapter, identifier = json.loads(experience.source[4:])
                        await self.db.execute(
                            "UPDATE incoming SET pending=0 WHERE adapter=? AND id=?", (adapter, identifier)
                        )
            await self.db.commit()
            for connection in self.nodes.values():
                if (connection.ready or connection is node) and not connection.ws.closed:
                    await connection.ws.send_json(
                        {
                            "kind": "history",
                            "entries": [entry.model_dump(mode="json") for entry in entries],
                            "complete": True,
                            "through": max((entry.gateway_sequence or 0 for entry in entries), default=0),
                        }
                    )
        elif kind in {"output", "handoff"}:
            if node.name != self.active or message["epoch"] != self.epoch:
                raise ValueError("Inactive Core")
            if kind == "handoff":
                target = self.nodes.get(message["target"])
                if target is None or not target.ready or time.monotonic() - target.last_seen >= CORE_TIMEOUT:
                    raise ValueError("The requested Core is unavailable")
                await self._elect(target.name)
            else:
                output: CoreToBotMessage = TypeAdapter(CoreToBotMessage).validate_python(message["message"])
                await self.bots.send_to_bot(output, target=message.get("target"))
        else:
            raise ValueError(f"Unsupported Core packet: {kind}")

    async def _connect(self, request: web.Request) -> web.WebSocketResponse:
        if request.headers.get("X-Auth-Token") != self.secret:
            raise web.HTTPUnauthorized()
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=MAX_SYNC_BYTES)
        await ws.prepare(request)
        name, origin = request.headers.get("X-Core-Name", ""), request.headers.get("X-Node-ID", "")
        if request.headers.get("X-Sync-Protocol") != SYNC_PROTOCOL or not name or not origin or name in self.nodes:
            await ws.send_json({"kind": "error", "detail": "Incompatible sync protocol or duplicate Core name"})
            await ws.close()
            return ws
        node = CoreConnection(name, origin, int(request.headers.get("X-Priority", "0")), ws, time.monotonic())
        self.nodes[name] = node
        try:
            async for packet in ws:
                if packet.type == WSMsgType.TEXT:
                    async with self._lock:
                        data: dict = {}
                        try:
                            node.last_seen = time.monotonic()
                            parsed = json.loads(packet.data)
                            if not isinstance(parsed, dict):
                                raise ValueError("A Core packet must be an object")
                            data = parsed
                            await self._packet(node, data)
                        except (ValueError, KeyError, TypeError) as exc:
                            await ws.send_json({"kind": "error", "detail": str(exc), "operation": data.get("kind")})
        finally:
            async with self._lock:
                self.nodes.pop(name, None)
                await self._elect()
        return ws


async def run(directory: Path, host: str, port: int) -> None:
    gateway = Gateway(directory, host, port, mas_config.ipc_secret)
    await gateway.start()
    try:
        await asyncio.Event().wait()
    finally:
        await gateway.stop()


def main() -> None:
    """启动常驻聊天入口，使用已有 IPC_SECRET 认证。"""
    parser = argparse.ArgumentParser(description="Muika always-on chat gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--directory", type=Path, default=Path("data/gateway"))
    args = parser.parse_args()
    asyncio.run(run(args.directory, args.host, args.port))


if __name__ == "__main__":
    main()
