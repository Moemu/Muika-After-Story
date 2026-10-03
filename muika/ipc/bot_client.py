"""框架无关的持久 Bot 客户端，连接固定入口并保留投递事实。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from muika.models import Resource
from muika.node.models import IncomingMessage, OutgoingMessage
from muika.node.resources import ResourceVault
from muika.utils.logger import logger

from .node_client import NodeClient
from .node_protocol import Acknowledge, Pending, Receive, Status

Delivery = Callable[[OutgoingMessage, list[Resource]], Awaitable[None]]
StatusCallback = Callable[[str], Awaitable[None]]


class DeliveryNotStarted(ConnectionError):
    """平台尚未开始发送，可在恢复连接后安全重试。"""


class BotSpool:
    """仅保存当前 Bot 的队列、原会话路由和投递记录。"""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(directory / "bot-queue.db")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS raw_input "
            "(id TEXT PRIMARY KEY, conversation TEXT NOT NULL, payload TEXT NOT NULL, "
            "received_at REAL NOT NULL, batch TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS outbound "
            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS delivery (id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL)"
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS route (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.execute("UPDATE delivery SET status='unknown' WHERE status='delivering'")
        self.db.commit()

    def queue(self, message: IncomingMessage) -> None:
        row = self.db.execute("SELECT payload FROM raw_input WHERE id=?", (message.id,)).fetchone()
        if row:
            previous = IncomingMessage.model_validate_json(row[0])
            if previous.model_dump(exclude={"occurred_at"}) != message.model_dump(exclude={"occurred_at"}):
                raise ValueError("Native message ID was reused with different content.")
            return
        self.db.execute(
            "INSERT INTO raw_input VALUES (?, ?, ?, ?, NULL)",
            (message.id, message.conversation_id, message.model_dump_json(), time.time()),
        )
        self.db.commit()

    def freeze(self, input_timeout: float) -> None:
        conversations = self.db.execute(
            "SELECT conversation, MAX(received_at) FROM raw_input WHERE batch IS NULL GROUP BY conversation"
        ).fetchall()
        for conversation, last_at in conversations:
            rows = self.db.execute(
                "SELECT id, payload FROM raw_input WHERE conversation=? AND batch IS NULL ORDER BY received_at, rowid",
                (conversation,),
            ).fetchall()
            if not rows:
                continue
            messages = [IncomingMessage.model_validate_json(row[1]) for row in rows]
            # 命令和生命周期事件立即冻结，并保留各自的身份。
            groups: list[list[IncomingMessage]] = []
            pending: list[IncomingMessage] = []
            for message in messages:
                if message.kind == "user_message":
                    pending.append(message)
                else:
                    if pending:
                        groups.append(pending)
                        pending = []
                    groups.append([message])
            if pending and time.time() - last_at >= input_timeout:
                groups.append(pending)
            for group in groups:
                if group[0].kind == "user_message":
                    member_ids = [message.id for message in group]
                    id = (
                        "batch:"
                        + hashlib.sha256((group[0].client_id + "\0" + "\0".join(member_ids)).encode()).hexdigest()
                    )
                    batch = group[0].model_copy(
                        update={
                            "id": id,
                            "text": "".join(message.text for message in group),
                            "resources": [reference for message in group for reference in message.resources],
                            "member_ids": member_ids,
                        }
                    )
                else:
                    batch = group[0]
                with self.db:
                    self.db.execute(
                        "INSERT OR IGNORE INTO outbound (id, payload) VALUES (?, ?)",
                        (batch.id, batch.model_dump_json()),
                    )
                    self.db.executemany(
                        "UPDATE raw_input SET batch=? WHERE id=?", [(batch.id, message.id) for message in group]
                    )

    def pending(self) -> list[IncomingMessage]:
        return [
            IncomingMessage.model_validate_json(row[0])
            for row in self.db.execute("SELECT payload FROM outbound WHERE accepted=0 ORDER BY rowid")
        ]

    def accepted(self, id: str) -> None:
        with self.db:
            self.db.execute("UPDATE outbound SET accepted=1 WHERE id=?", (id,))

    def status(self, reply: OutgoingMessage) -> str:
        row = self.db.execute("SELECT payload, status FROM delivery WHERE id=?", (reply.id,)).fetchone()
        if row:
            if row[0] != reply.model_dump_json():
                raise ValueError("Reply identity was reused with different content.")
            return row[1]
        with self.db:
            self.db.execute("INSERT INTO delivery VALUES (?, ?, 'received')", (reply.id, reply.model_dump_json()))
        return "received"

    def mark_delivery(self, id: str, status: str) -> None:
        if status not in {"received", "delivering", "delivered", "unknown"}:
            raise ValueError("Invalid delivery status.")
        with self.db:
            self.db.execute("UPDATE delivery SET status=? WHERE id=?", (status, id))

    def resolve_delivery(self, id: str, delivered: bool) -> None:
        """根据用户核对结果恢复投递；调用前需停止对应 Bot。"""
        row = self.db.execute("SELECT status FROM delivery WHERE id=?", (id,)).fetchone()
        if row is None or row[0] != "unknown":
            raise ValueError("Only unknown deliveries can be reconciled.")
        self.mark_delivery(id, "delivered" if delivered else "received")

    def save_route(self, id: str, payload: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO route VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload", (id, payload)
            )

    def route(self, id: str) -> str:
        row = self.db.execute("SELECT payload FROM route WHERE id=?", (id,)).fetchone()
        if row is None:
            raise ValueError("Conversation route is not available on this Bot.")
        return row[0]

    def close(self) -> None:
        self.db.close()


class DurableBotClient:
    """永久重连；先保存输入，再冻结批次，投递成功后确认回复。"""

    def __init__(
        self,
        address: str,
        token: str,
        client_id: str,
        directory: Path,
        deliver: Delivery,
        *,
        input_timeout: float = 0,
        on_status: StatusCallback | None = None,
        ca_file: Path | None = None,
    ) -> None:
        self.address, self.token, self.client_id, self.deliver = address, token, client_id, deliver
        self.input_timeout, self.on_status = input_timeout, on_status
        self.spool = BotSpool(directory)
        self.vault = ResourceVault(directory / "resources")
        self.connected = asyncio.Event()
        self.stopping = asyncio.Event()
        self._closed = False
        self.ca_file = ca_file
        self.core_available = False

    async def queue_input(self, message: IncomingMessage, resources: list[Resource] | None = None) -> None:
        """复制附件并保存原始输入，离线时仍可调用。"""
        if message.client_id != self.client_id:
            raise ValueError("Input identity differs from this Bot.")
        if resources:
            message = message.model_copy(
                update={"resources": [self.vault.preserve(resource) for resource in resources]}
            )
        self.spool.queue(message)

    async def status_changed(self, status: str) -> None:
        if self.on_status is not None:
            await self.on_status(status)

    async def run(self) -> None:
        delay = 1.0
        while not self.stopping.is_set():
            try:
                async with NodeClient(self.address, self.token, ca_file=self.ca_file) as client:
                    self.connected.set()
                    delay = 1.0
                    await self.status_changed("connected")
                    next_status = 0.0
                    while not self.stopping.is_set():
                        if time.monotonic() >= next_status:
                            service_status = await client.request(Status())
                            available = service_status.lease is not None
                            if available != self.core_available:
                                await self.status_changed("core_available" if available else "core_unavailable")
                            self.core_available = available
                            next_status = time.monotonic() + 1
                        self.spool.freeze(self.input_timeout)
                        for message in self.spool.pending():
                            for reference in message.resources:
                                await client.upload_resource(self.vault.materialize(reference), self.vault)
                            await client.request(Receive(message=message))
                            self.spool.accepted(message.id)
                        for reply in (await client.request(Pending())).replies:
                            status = self.spool.status(reply)
                            if status == "delivered":
                                await client.request(Acknowledge(message_id=reply.id))
                                continue
                            if status in {"unknown", "delivering"}:
                                continue
                            resources = [
                                await client.download_resource(reference, self.vault) for reference in reply.resources
                            ]
                            self.spool.mark_delivery(reply.id, "delivering")
                            try:
                                await self.deliver(reply, resources)
                            except DeliveryNotStarted:
                                self.spool.mark_delivery(reply.id, "received")
                                break
                            except BaseException:
                                self.spool.mark_delivery(reply.id, "unknown")
                                await self.status_changed("delivery_unknown")
                                raise
                            self.spool.mark_delivery(reply.id, "delivered")
                            await client.request(Acknowledge(message_id=reply.id))
                        await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[Bot] State connection unavailable: {type(exc).__name__}: {exc}")
                await self.status_changed("offline")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
            finally:
                self.connected.clear()
                self.core_available = False

    async def close(self) -> None:
        self.stopping.set()
        if not self._closed:
            self.spool.close()
            self._closed = True
