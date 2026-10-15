"""同步本地活动并接受在线角色；入口失联时由指定 PC 继续本地活动。"""

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict
from functools import partial
from uuid import uuid4

if sys.version_info >= (3, 11):
    from asyncio import timeout
else:
    from async_timeout import timeout

import aiohttp
from sqlalchemy import select

from muika.config import mas_config
from muika.core.events import ScheduledTriggerPayload
from muika.core.executor import SendReceipt
from muika.core.state import MuikaState
from muika.database.db import get_session, observe_commits
from muika.database.orm_models import SyncEventORM, SyncStateORM
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
        self.sync_error: str | None = None
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
        self._history_requested = False
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
        already_closing = self._closing
        self._closing = True
        if self._task is not None:
            if not already_closing and (self.connected or self.ws is None):
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        observe_commits(None)

    async def _set_role(self, active: bool, reason: str) -> None:
        self.active = active
        await self._await_business_completion(self.role(active, reason))

    @staticmethod
    async def _await_business_completion(callback: Awaitable[None]) -> None:
        """连接关闭时等待已开始的角色切换或输入处理完成。"""
        transition = asyncio.ensure_future(callback)
        try:
            await asyncio.shield(transition)
        except asyncio.CancelledError:
            await transition
            raise

    async def send(self, message: CoreToBotMessage, target: str | None = None) -> SendReceipt:
        """在线输出带任期；此发送不等待平台确认，写入网关连接即算 ``written``。"""
        if not self.active or not self.connected or self.ws is None:
            return SendReceipt.FAILED
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
            return SendReceipt.WRITTEN
        except (aiohttp.ClientError, OSError, ValueError) as exc:
            logger.error(f"[CoreLink] Could not send output: {exc}")
            return SendReceipt.FAILED

    async def handoff(self, target: str) -> None:
        """请求已在线的 Core 接管，结果通过角色事件返回。"""
        if not self.active or not self.connected or self.ws is None or target not in self.nodes:
            raise ValueError("The requested device is unavailable")
        await self.memory.add_material("agent", f"Core handoff requested to {target}; completion is not yet observed.")
        try:
            async with timeout(15):
                while any(entry.activity.id not in self._sent for entry in await self.store.entries(pending_only=True)):
                    await self._publish()
            await self.ws.send_json({"kind": "handoff", "target": target, "epoch": self.epoch})
        except (aiohttp.ClientError, OSError, TimeoutError) as exc:
            await self.memory.add_material("agent", f"Core handoff request failed: {exc}")
            raise

    async def relay_reminder(self, payload: ScheduledTriggerPayload) -> bool:
        """在线时交由入口的持久事件队列转发，离线活动设备保持本地提醒。"""
        if not self.connected or self.ws is None:
            return not self.active
        if self.active:
            try:
                await self.ws.send_json({"kind": "reminder", "epoch": self.epoch, "payload": asdict(payload)})
            except (aiohttp.ClientError, OSError) as exc:
                logger.warning(f"[CoreLink] Could not forward reminder: {exc}")
                await self.ws.close()
        return True

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

    async def _request_history(self, after: int) -> None:
        """请求 after 之后的增量历史，并把下一条历史回复标记为本次请求的回答。"""
        assert self.ws is not None
        self._history_requested = True
        await self.ws.send_json({"kind": "history", "after": after})

    async def _history(self, packet: dict) -> None:
        assert self.ws is not None
        requested, self._history_requested = self._history_requested, False
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
            await self._request_history(packet["through"])
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
                ):
                    await self.store.apply(pending[0].model_copy(update={"gateway_sequence": 0}))
            if self._preserve and not self._captured:
                await self.store.capture_snapshot()
                self._captured = True
            if not await self._publish():
                cursor = await self.store.cursor()
                # 请求回答已枚举游标之后的全部活动；残留空洞在网关中不存在，补请求拿不到。
                if packet.get("through") and cursor < packet["through"] and not requested:
                    await self._request_history(cursor)
                    return
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

    async def _consume_packets(self, packets: asyncio.Queue[dict]) -> None:
        """按连接内顺序应用历史、切换角色并处理输入。"""
        await self._set_role(False, "")
        while True:
            packet = await packets.get()
            if packet["kind"] == "history":
                await self._history(packet)
            elif packet["kind"] == "role":
                # 只认“我以外”的设备：单机隧道抖动后重连不是活动位置变化。
                known_other = any(node != self.name for node in self.nodes)
                self.epoch, self.nodes = packet["epoch"], packet["nodes"]
                if not self._joining:
                    self.sync_error = None
                    active = packet["active"] == self.name
                    if active != self.active:
                        inherited_history = False
                        if active and not known_other and self.memory.has_history:
                            async with get_session(record_activity=False) as db:
                                inherited_history = (
                                    await db.scalar(
                                        select(SyncEventORM.id).where(SyncEventORM.origin != self.store.origin).limit(1)
                                    )
                                    is not None
                                )
                        await self._set_role(
                            active,
                            (
                                "Your companionship is now continuing on this device."
                                if known_other or inherited_history
                                else ""
                            ),
                        )
            elif packet["kind"] == "input" and self.active and packet["epoch"] == self.epoch:
                await self._await_business_completion(self.receive(packet["message"], packet["adapter"]))
            elif packet["kind"] == "error":
                if packet.get("operation") == "output":
                    logger.error(f"[CoreLink] {packet['detail']}")
                    continue
                if packet.get("operation") == "handoff":
                    logger.warning(f"[CoreLink] Core handoff failed: {packet['detail']}")
                    await self._set_role(self.active, "")
                    continue

    async def _run(self) -> None:
        retry_delay = 2.0
        while not self._closing:
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
                    self.active = False
                    self._preserve = self._foreground
                    self._captured = False
                    self.connected, self._joining = True, True
                    self._sent.clear()
                    self._history_requested = False
                    packets: asyncio.Queue[dict] = asyncio.Queue()
                    consumer = asyncio.create_task(self._consume_packets(packets))
                    workers = [consumer, asyncio.create_task(self._pulse()), asyncio.create_task(self._flush())]
                    for worker in workers:
                        worker.add_done_callback(partial(self._worker_finished, ws=self.ws))
                    await self._request_history(await self.store.cursor())
                    async for message in self.ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            break
                        packet = message.json()
                        if packet["kind"] == "error" and packet.get("operation") not in {"output", "handoff"}:
                            raise ValueError(packet["detail"])
                        packets.put_nowait(packet)
                    if consumer.done():
                        await consumer
            except (aiohttp.ClientError, OSError, TimeoutError) as exc:
                logger.warning(f"[CoreLink] Gateway unavailable: {exc}")
            except Exception as exc:
                invalid = True
                error = f"{type(exc).__name__}: {exc}"
                if error != self.sync_error:
                    logger.exception(f"[CoreLink] Could not synchronize local history: {error}")
                else:
                    logger.debug(f"[CoreLink] Local history still needs repair: {error}")
                self.sync_error = error
            finally:
                self.connected = False
                for worker in workers:
                    worker.cancel()
                results = await asyncio.gather(*workers, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception) and not isinstance(
                        result, (aiohttp.ClientError, OSError, TimeoutError)
                    ):
                        invalid = True
                        self.sync_error = f"{type(result).__name__}: {result}"
                self.connected, self.ws = False, None
                # 断线与同步失败只改变传输状态，不是活动位置变化：不产生人格事件，
                # 否则隧道每次抖动都会让她播报一遍通道。
                await self._set_role(
                    mas_config.local_fallback and not invalid and self.sync_error is None and not self._closing, ""
                )
            if self._closing:
                return
            if self.sync_error:
                logger.debug(f"[CoreLink] Retrying synchronization in {retry_delay:g} seconds.")
                await asyncio.sleep(retry_delay)
                retry_delay = min(30.0, retry_delay * 2)
            else:
                retry_delay = 2.0
                await asyncio.sleep(2)
