"""在短事务中管理 Core 控制权和可靠收发记录。"""

import asyncio
import hashlib
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from muika.database.db import business_transaction, get_session
from muika.database.orm_models import (
    RuntimeAuthorityORM,
    RuntimeInboxORM,
    RuntimeOutboxORM,
    RuntimeStateORM,
)

from .event_protocol import RuntimeEvent
from .models import ClaimedMessage, CoreLease, IncomingMessage, OutgoingMessage
from .turn_protocol import ClientRoute, HandoffRecord, RuntimeSnapshot


class JournalConflict(ValueError):
    """表示身份、处理权或重传内容与持久记录不符。"""


class RuntimeJournal:
    """由单一状态服务持有，序列化短事务并使用单调时钟判定期限。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._deadline = 0.0
        self._started = False
        self._preferred_owner: str | None = None
        self._preference_deadline = 0.0

    async def start(self) -> None:
        """恢复部署身份，并使前一个服务运行期的控制权失效。"""
        async with self._lock:
            if self._started:
                return
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                authority = await db.get(RuntimeAuthorityORM, 1)
                if authority is None:
                    db.add(RuntimeAuthorityORM(id=1, cluster_id=uuid4().hex, epoch=1, owner=""))
                else:
                    authority.epoch += 1
                    authority.owner = ""
                    if authority.handoff:
                        handoff = HandoffRecord.model_validate_json(authority.handoff)
                        self._preferred_owner = handoff.target
                        self._preference_deadline = time.monotonic() + min(5, max(0, handoff.expires_at - time.time()))
            self._started = True

    async def _observe(
        self, db: AsyncSession, identity: str, event: RuntimeEvent, route: ClientRoute | None = None
    ) -> None:
        if route is None:
            runtime = await db.get(RuntimeStateORM, 1)
            route = RuntimeSnapshot.model_validate_json(runtime.payload).route if runtime else None
        if route is None:
            return
        existing = await db.scalar(
            select(RuntimeInboxORM).where(
                RuntimeInboxORM.client_id == route.client_id, RuntimeInboxORM.message_id == identity
            )
        )
        if existing is not None:
            return
        message = IncomingMessage(
            id=identity,
            client_id=route.client_id,
            conversation_id=route.conversation_id,
            kind="runtime_event",
            event=event,
            occurred_at=event.timestamp,
        )
        db.add(RuntimeInboxORM(client_id=message.client_id, message_id=identity, payload=message.model_dump_json()))

    async def observe(self, event: RuntimeEvent) -> None:
        """持久保存状态服务确认的设备变化，不依赖活动 Core 的轮询。"""
        async with self.truth_transaction() as db:
            await self._observe(db, f"observation:{uuid4().hex}", event)

    async def _handoff_result(
        self, db: AsyncSession, identity: str, report: str, status: str, route: ClientRoute | None = None
    ) -> None:
        await self._observe(
            db,
            f"{identity}:result",
            RuntimeEvent(type="core_handoff_result", timestamp=datetime.now(), report=report, status=status),
            route,
        )

    async def _close_pending_handoff(self, db: AsyncSession) -> None:
        runtime = await db.get(RuntimeStateORM, 1)
        if runtime is None:
            return
        snapshot = RuntimeSnapshot.model_validate_json(runtime.payload)
        if snapshot.handoff_target is None:
            return
        await self._handoff_result(
            db,
            snapshot.handoff_id or f"handoff:interrupted:{snapshot.handoff_target}",
            f"Core handoff to {snapshot.handoff_target} failed: the request was interrupted before transfer. "
            "The pending request was closed during recovery.",
            "failed",
            snapshot.route,
        )
        snapshot.handoff_target, snapshot.handoff_id = None, None
        runtime.payload = snapshot.model_dump_json()

    async def _authority(self, db: AsyncSession) -> RuntimeAuthorityORM:
        if not self._started:
            raise RuntimeError("Runtime journal has not started.")
        authority = await db.get(RuntimeAuthorityORM, 1)
        if authority is None:
            raise RuntimeError("Runtime authority is missing.")
        return authority

    async def _require_owner(self, db: AsyncSession, owner: str, epoch: int) -> RuntimeAuthorityORM:
        authority = await self._authority(db)
        if authority.owner != owner or authority.epoch != epoch or time.monotonic() >= self._deadline:
            raise JournalConflict("Core lease has expired or changed.")
        return authority

    @asynccontextmanager
    async def transaction(self, owner: str, epoch: int):
        """在短业务事务的开始和提交边界校验活动任期。"""
        async with self._lock:
            async with business_transaction() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                await self._require_owner(db, owner, epoch)
                yield db
                await self._require_owner(db, owner, epoch)

    @asynccontextmanager
    async def truth_transaction(self):
        """保存原设备的事实结果，允许报告已经失效的任期。"""
        async with self._lock:
            async with business_transaction() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                yield db

    @asynccontextmanager
    async def execution_transaction(self, epoch: int):
        """设备只能校验已授予的任期，不能自行取得控制权。"""
        async with self._lock:
            async with business_transaction() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                authority = await self._authority(db)
                owner = authority.owner
                await self._require_owner(db, owner, epoch)
                yield db
                await self._require_owner(db, owner, epoch)

    @staticmethod
    def _validate_duration(duration: float) -> None:
        if not math.isfinite(duration) or duration <= 0 or duration > 300:
            raise ValueError("Lease duration must be positive and at most 300 seconds.")

    async def acquire(self, owner: str, duration: float, candidates: list[str] | None = None) -> CoreLease | None:
        """仅在原租约失效后授予新任期。"""
        self._validate_duration(duration)
        if not owner:
            raise ValueError("Core owner must not be empty.")
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                authority = await self._authority(db)
                if authority.owner and time.monotonic() < self._deadline:
                    return None
                if (
                    self._preferred_owner
                    and time.monotonic() < self._preference_deadline
                    and owner != self._preferred_owner
                ):
                    return None
                if (
                    (not self._preferred_owner or time.monotonic() >= self._preference_deadline)
                    and candidates
                    and owner != candidates[0]
                ):
                    return None
                if authority.handoff:
                    handoff = HandoffRecord.model_validate_json(authority.handoff)
                    handoff.granted_epoch = authority.epoch + 1
                    authority.handoff = handoff.model_dump_json()
                else:
                    await self._close_pending_handoff(db)
                authority.epoch += 1
                authority.owner = owner
                grant = CoreLease(
                    cluster_id=authority.cluster_id, owner=owner, epoch=authority.epoch, remaining_seconds=duration
                )
            self._deadline = time.monotonic() + duration
            self._preferred_owner = None
            return grant

    async def current_lease(self) -> CoreLease | None:
        """返回服务器时钟判定的活动位置，客户端不自行延长有效期。"""
        async with self._lock:
            async with get_session() as db:
                authority = await self._authority(db)
                remaining = self._deadline - time.monotonic()
                if not authority.owner or remaining <= 0:
                    return None
                return CoreLease(
                    cluster_id=authority.cluster_id,
                    owner=authority.owner,
                    epoch=authority.epoch,
                    remaining_seconds=remaining,
                )

    async def handoff(self, owner: str, epoch: int, target: str, rejection: str | None = None) -> bool:
        """撤销原任期并短暂保留给目标，目标消失时仍可正常回退。"""
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                authority = await self._require_owner(db, owner, epoch)
                runtime = await db.get(RuntimeStateORM, 1)
                snapshot = RuntimeSnapshot.model_validate_json(runtime.payload) if runtime else RuntimeSnapshot()
                identity = snapshot.handoff_id or f"handoff:{epoch}:{target}"
                if rejection is not None:
                    await self._handoff_result(
                        db,
                        identity,
                        f"Core handoff {owner} -> {target} failed: {rejection} The current Core stays active.",
                        "failed",
                        snapshot.route,
                    )
                else:
                    authority.owner = ""
                    authority.handoff = HandoffRecord(
                        id=identity,
                        source=owner,
                        target=target,
                        route=snapshot.route,
                        expires_at=time.time() + 5,
                    ).model_dump_json()
                if runtime is not None:
                    snapshot.handoff_target, snapshot.handoff_id = None, None
                    runtime.payload = snapshot.model_dump_json()
            if rejection is not None:
                return False
            self._deadline = 0
            self._preferred_owner, self._preference_deadline = target, time.monotonic() + 5
            return True

    async def ready(self, owner: str, epoch: int) -> None:
        """恢复完成后确认交接结果，不把取得租约当作恢复成功。"""
        async with self.transaction(owner, epoch) as db:
            authority = await self._authority(db)
            if authority.handoff is None:
                return
            handoff = HandoffRecord.model_validate_json(authority.handoff)
            if handoff.granted_epoch != epoch:
                raise JournalConflict("Core readiness does not match its handoff grant.")
            completed = owner == handoff.target
            report = (
                f"Core handoff {handoff.source} -> {handoff.target} completed. "
                f"You resumed on {owner} with your saved memory and action state."
                if completed
                else f"Core handoff {handoff.source} -> {handoff.target} failed: the target did not complete recovery. "
                f"You resumed on fallback {owner} with your saved memory and action state."
            )
            await self._handoff_result(db, handoff.id, report, "completed" if completed else "failed", handoff.route)
            authority.handoff = None

    async def renew(self, owner: str, epoch: int, duration: float) -> CoreLease:
        """延长当前有效任期，不复活已经失效的租约。"""
        self._validate_duration(duration)
        async with self._lock:
            async with get_session() as db:
                authority = await self._require_owner(db, owner, epoch)
                grant = CoreLease(cluster_id=authority.cluster_id, owner=owner, epoch=epoch, remaining_seconds=duration)
            self._deadline = time.monotonic() + duration
            return grant

    async def release(self, owner: str, epoch: int) -> None:
        """释放当前控制权，故障接管不依赖此操作。"""
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                authority = await self._require_owner(db, owner, epoch)
                authority.owner = ""
            self._deadline = 0.0

    async def receive(self, message: IncomingMessage) -> int:
        """持久接收输入，拒绝同一身份携带不同内容。"""
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                row = await db.scalar(
                    select(RuntimeInboxORM).where(
                        RuntimeInboxORM.client_id == message.client_id, RuntimeInboxORM.message_id == message.id
                    )
                )
                payload = message.model_dump_json()
                if row is not None:
                    if row.payload != payload:
                        raise JournalConflict("Input ID was reused with different content.")
                    return row.sequence
                row = RuntimeInboxORM(client_id=message.client_id, message_id=message.id, payload=payload)
                db.add(row)
                await db.flush()
                return row.sequence

    async def publish(self, owner: str, epoch: int, message: IncomingMessage) -> int:
        """保存内部事件；同一业务通知的重投沿用首次发生时间。"""
        if message.kind != "runtime_event" or message.event is None:
            raise ValueError("An internal event payload is required.")
        async with self.transaction(owner, epoch) as db:
            row = await db.scalar(
                select(RuntimeInboxORM).where(
                    RuntimeInboxORM.client_id == message.client_id, RuntimeInboxORM.message_id == message.id
                )
            )
            if row is not None:
                previous = IncomingMessage.model_validate_json(row.payload)
                current = message.model_copy(deep=True)
                current.occurred_at = previous.occurred_at
                if current.event is not None and previous.event is not None:
                    current.event.timestamp = previous.event.timestamp
                if current != previous:
                    raise JournalConflict("Internal event identity was reused with different content.")
                return row.sequence
            row = RuntimeInboxORM(client_id=message.client_id, message_id=message.id, payload=message.model_dump_json())
            db.add(row)
            await db.flush()
            return row.sequence

    async def claim(self, owner: str, epoch: int) -> ClaimedMessage | None:
        """认领最早未完成输入，返回原认领以恢复丢失的响应。"""
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                await self._require_owner(db, owner, epoch)
                row = await db.scalar(
                    select(RuntimeInboxORM)
                    .where(RuntimeInboxORM.status != "processed")
                    .order_by(RuntimeInboxORM.sequence)
                    .limit(1)
                )
                if row is None:
                    return None
                row.owner, row.epoch, row.status = owner, epoch, "claimed"
                return ClaimedMessage(
                    sequence=row.sequence,
                    message=IncomingMessage.model_validate_json(row.payload),
                    owner=owner,
                    epoch=epoch,
                )

    async def commit(self, owner: str, epoch: int, claim: ClaimedMessage, replies: list[OutgoingMessage]) -> None:
        """原子保存处理完成与发件箱；人格事务接入前不用于生产对话。"""
        if len({reply.id for reply in replies}) != len(replies):
            raise JournalConflict("Reply IDs must be unique within a commit.")
        digest = hashlib.sha256(
            json.dumps(
                [reply.model_dump(mode="json") for reply in replies], sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                await self._require_owner(db, owner, epoch)
                row = await db.get(RuntimeInboxORM, claim.sequence)
                if row is None or row.owner != owner or row.epoch != epoch:
                    raise JournalConflict("Input is not claimed by this Core.")
                if row.payload != claim.message.model_dump_json() or claim.owner != owner or claim.epoch != epoch:
                    raise JournalConflict("Claim does not match the saved input.")
                if row.status == "processed":
                    if row.commit_digest != digest:
                        raise JournalConflict("Input was already committed with different replies.")
                    return
                for reply in replies:
                    if (
                        reply.client_id != claim.message.client_id
                        or reply.conversation_id != claim.message.conversation_id
                    ):
                        raise JournalConflict("Reply target differs from its input.")
                    existing = await db.scalar(select(RuntimeOutboxORM).where(RuntimeOutboxORM.message_id == reply.id))
                    if existing is not None:
                        raise JournalConflict("Reply ID is already in use.")
                    db.add(
                        RuntimeOutboxORM(
                            message_id=reply.id, client_id=reply.client_id, payload=reply.model_dump_json()
                        )
                    )
                row.status, row.commit_digest = "processed", digest

    async def pending(self, client_id: str, limit: int = 100) -> list[OutgoingMessage]:
        """读取指定客户端尚未确认的回复。"""
        if not 1 <= limit <= 1000:
            raise ValueError("Pending limit must be between 1 and 1000.")
        async with get_session() as db:
            rows = await db.scalars(
                select(RuntimeOutboxORM)
                .where(RuntimeOutboxORM.client_id == client_id, RuntimeOutboxORM.acknowledged.is_(False))
                .order_by(RuntimeOutboxORM.sequence)
                .limit(limit)
            )
            return [OutgoingMessage.model_validate_json(row.payload) for row in rows]

    async def emit(self, db: AsyncSession, message: OutgoingMessage) -> None:
        """按副作用身份保存独立主动消息，重传必须保持内容一致。"""
        existing = await db.scalar(select(RuntimeOutboxORM).where(RuntimeOutboxORM.message_id == message.id))
        if existing is not None:
            if existing.payload != message.model_dump_json():
                raise JournalConflict("Message identity was reused with different content.")
            return
        db.add(RuntimeOutboxORM(message_id=message.id, client_id=message.client_id, payload=message.model_dump_json()))

    async def acknowledge(self, client_id: str, message_id: str) -> None:
        """幂等保存客户端投递确认，拒绝确认其他客户端的回复。"""
        async with self._lock:
            async with get_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                row = await db.scalar(select(RuntimeOutboxORM).where(RuntimeOutboxORM.message_id == message_id))
                if row is None or row.client_id != client_id:
                    raise JournalConflict("Reply does not belong to this client.")
                row.acknowledged = True
