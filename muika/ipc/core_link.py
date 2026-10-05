"""同步本地活动并接受在线角色；入口失联时由指定 PC 继续本地活动。"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from functools import partial
from uuid import uuid4

import aiohttp

from muika.config import mas_config
from muika.core.state import MuikaState
from muika.database.db import get_session, observe_commits
from muika.database.orm_models import SyncStateORM
from muika.models import Resource
from muika.utils.logger import logger

from .attachments import AttachmentTransfer
from .protocol import CommandResult, CoreToBotMessage, SendMessage
from .sync_models import MAX_SYNC_BYTES, SYNC_PROTOCOL, Attachment, SyncEntry
from .sync_store import SyncStore

RoleCallback = Callable[[bool, str], Awaitable[None]]
InputCallback = Callable[[dict, str], Awaitable[None]]


class CoreLink:
    """只传递事件和已保存结果，不提供远程数据库或工具接口。"""

    def __init__(
        self, state: MuikaState, role: RoleCallback, receive: InputCallback, advance: Callable[[float], None]
    ) -> None:
        assert state.memory is not None
        self.state, self.memory = state, state.memory
        self.role, self.receive, self.advance = role, receive, advance
        self.name = mas_config.core_node_name
        self.active = self.connected = False
        self.epoch = 0
        self.nodes: list[str] = []
        self.store: SyncStore
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._publish_lock = asyncio.Lock()
        self._preserve = False
        self._joining = True
        self._sent: set[str] = set()
        self._closing = False
        self._captured = False
        self._foreground = False
        self.attachments = AttachmentTransfer(
            mas_config.gateway_url, mas_config.ipc_secret, mas_config.data_dir / "chat_attachments"
        )

    async def start(self) -> None:
        """沿用数据库中的节点身份，不根据设备名重建主键。"""
        async with get_session(record_activity=False) as db:
            saved = await db.get(SyncStateORM, "origin")
            if saved is None:
                saved = SyncStateORM(key="origin", payload=uuid4().hex)
                db.add(saved)
            self.store = SyncStore(saved.payload)
            self.store.state = self.state
        await self.store.initialize()
        observe_commits(self.store.record)
        self._task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        observe_commits(None)

    async def _set_role(self, active: bool, reason: str) -> None:
        self.active = active
        await self.role(active, reason)

    async def send(self, message: CoreToBotMessage, target: str | None = None) -> bool:
        """在线输出带任期；此发送不等待平台确认。"""
        if not self.active or not self.connected or self.ws is None:
            return False
        try:
            if isinstance(message, (SendMessage, CommandResult)):
                message = message.model_copy(
                    update={
                        "resources": [await self.attachments.upload(Resource(**item)) for item in message.resources]
                    }
                )
            await self.ws.send_json(
                {"kind": "output", "epoch": self.epoch, "message": message.model_dump(mode="json"), "target": target}
            )
            return True
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            logger.error(f"[CoreLink] Could not send output: {exc}")
            return False

    async def handoff(self, target: str) -> None:
        """请求已在线的 Core 接管，结果通过角色事件返回。"""
        if not self.active or not self.connected or self.ws is None or target not in self.nodes:
            raise ValueError("The requested device is unavailable")
        await self.memory.add_material("agent", f"Core handoff requested to {target}; completion is not yet observed.")
        try:
            async with asyncio.timeout(15):
                while any(entry.activity.id not in self._sent for entry in await self.store.entries(pending_only=True)):
                    await self._publish()
            await self.ws.send_json({"kind": "handoff", "target": target, "epoch": self.epoch})
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            await self.memory.add_material("agent", f"Core handoff request failed: {exc}")
            raise

    async def _publish(self) -> bool:
        async with self._publish_lock:
            assert self.ws is not None
            pending = await self.store.entries(pending_only=True)
            batch = [entry.activity for entry in pending if entry.activity.id not in self._sent][:64]
            size = 0
            for index, activity in enumerate(batch):
                size += len(activity.model_dump_json().encode())
                if index and size > MAX_SYNC_BYTES // 2:
                    batch = batch[:index]
                    break
            if batch:
                batch = [item.model_copy(deep=True) for item in batch]
                for item in batch:
                    for experience in item.experiences:
                        experience.resources = [
                            Attachment.model_validate(await self.attachments.upload(Resource(**r.model_dump())))
                            for r in experience.resources
                        ]
                await self.ws.send_json(
                    {"kind": "publish", "activities": [item.model_dump(mode="json") for item in batch]}
                )
                self._sent.update(item.id for item in batch)
            return bool(pending)

    async def _history(self, packet: dict) -> None:
        assert self.ws is not None
        changed = False
        for raw in packet["entries"]:
            entry = SyncEntry.model_validate(raw)
            if entry.activity.origin != self.store.origin:
                for experience in entry.activity.experiences:
                    experience.resources = [
                        Attachment.model_validate(
                            (await self.attachments.download(Resource(**r.model_dump()))).to_dict()
                        )
                        for r in experience.resources
                    ]
            changed |= await self.store.apply(entry, preserve_state=self._preserve or self.active)
            self._sent.discard(entry.activity.id)
        if changed:
            await self.memory.load(record_activity=False)
        if not packet["complete"]:
            await self.ws.send_json({"kind": "history", "after": packet["through"]})
            return
        if self._joining:
            pending = await self.store.entries(pending_only=True)
            if packet["through"] and len(pending) == 1:
                baseline = pending[0].activity
                if (
                    baseline.snapshot is not None
                    and baseline.snapshot.first_interaction_at is None
                    and not baseline.snapshot.state.mood
                    and not baseline.experiences
                    and not baseline.facts
                    and not baseline.diaries
                    and not baseline.tasks
                    and not baseline.calls
                ):
                    await self.store.apply(pending[0].model_copy(update={"gateway_sequence": 0}))
            if self._preserve and not self._captured:
                await self.store.capture_snapshot()
                self._captured = True
            if not await self._publish():
                self._joining = False
                self._preserve = False
                await self.ws.send_json({"kind": "ready", "foreground": self._foreground})
        else:
            self.store.changed.set()

    async def _pulse(self) -> None:
        assert self.ws is not None
        last = time.monotonic()
        while True:
            await self.ws.send_json({"kind": "heartbeat"})
            now = time.monotonic()
            self.advance(now - last)
            last = now
            await asyncio.sleep(2)

    async def _flush(self) -> None:
        while True:
            await self.store.changed.wait()
            self.store.changed.clear()
            if not self._joining:
                await self._publish()

    @staticmethod
    def _worker_finished(task: asyncio.Task[None], ws: aiohttp.ClientWebSocketResponse) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error(f"[CoreLink] Synchronization stopped: {error}")
            asyncio.create_task(ws.close())

    async def _run(self) -> None:
        while True:
            workers: list[asyncio.Task[None]] = []
            invalid = False
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
                    self.ws = await session.ws_connect(
                        mas_config.gateway_url,
                        headers={
                            "X-Auth-Token": mas_config.ipc_secret,
                            "X-Core-Name": self.name,
                            "X-Node-ID": self.store.origin,
                            "X-Sync-Protocol": SYNC_PROTOCOL,
                            "X-Priority": str(mas_config.core_priority),
                        },
                        heartbeat=20,
                        max_msg_size=MAX_SYNC_BYTES,
                    )
                    self._foreground = self.active and mas_config.local_fallback
                    self._preserve = self._foreground
                    self._captured = False
                    self.connected, self._joining = True, True
                    self._sent.clear()
                    await self._set_role(False, "Synchronizing saved histories")
                    workers = [asyncio.create_task(self._pulse()), asyncio.create_task(self._flush())]
                    for worker in workers:
                        worker.add_done_callback(partial(self._worker_finished, ws=self.ws))
                    await self.ws.send_json({"kind": "history", "after": await self.store.cursor()})
                    async for message in self.ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        packet = message.json()
                        if packet["kind"] == "history":
                            await self._history(packet)
                        elif packet["kind"] == "role":
                            self.epoch, self.nodes = packet["epoch"], packet["nodes"]
                            if not self._joining:
                                await self._set_role(
                                    packet["active"] == self.name,
                                    f"Active device: {packet['active']}; available: {self.nodes}",
                                )
                        elif packet["kind"] == "input" and self.active and packet["epoch"] == self.epoch:
                            await self.receive(packet["message"], packet["adapter"])
                        elif packet["kind"] == "error":
                            logger.error(f"[CoreLink] {packet['detail']}")
                            if packet.get("operation") == "output":
                                continue
                            if packet.get("operation") == "handoff":
                                await self._set_role(self.active, f"Core handoff failed: {packet['detail']}")
                                continue
                            invalid = True
                            break
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                logger.warning(f"[CoreLink] Gateway unavailable: {exc}")
            except Exception as exc:
                invalid = True
                logger.exception(f"[CoreLink] Could not synchronize local history: {exc}")
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                self.connected, self.ws = False, None
                await self._set_role(
                    mas_config.local_fallback and not invalid and not self._closing,
                    (
                        "Gateway unavailable; local companionship continues"
                        if mas_config.local_fallback
                        else "Gateway unavailable; waiting for reconnection"
                    ),
                )
            await asyncio.sleep(2)
