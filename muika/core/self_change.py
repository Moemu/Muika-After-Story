"""内核变更自我感知：检测层记账，投递层决定她何时开口。

变更事实先持久化进"待感知账本"（``system_state`` 表 ``self_change`` 键），
调度器在沉降、间隔与硬条件满足时把账本冻结为一次 :class:`SelfChangedEvent`
交给她本人消化。她自己的修改（origin=self）只推进基线、不入账；命令重载
只写一条简短记忆事实。整体承诺为至少一次感知：发送成功才销账，失败退避
重试，宁可偶尔重复也不丢失察觉。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Optional

from muika.config import mas_config
from muika.core.brain import FALLBACK_REPLY
from muika.core.events import SelfChangedEvent, SelfChangedPayload
from muika.core.executor import SendReceipt
from muika.core.self_mod.fingerprint import (
    compare_versions,
    compute_kernel_files,
    compute_plugin_digest,
    kernel_root,
)
from muika.core.self_mod.proposals import is_core_maintenance_active
from muika.database.db import get_session
from muika.database.orm_models import SystemStateORM
from muika.plugin.loader import get_plugins
from muika.plugin.manager import is_builtin_plugin
from muika.utils.logger import logger
from muika.utils.utils import get_version

from .constants import (
    SELF_CHANGE_BATCH_MAX_AGE,
    SELF_CHANGE_RETRY_BASE_SECONDS,
    SELF_CHANGE_RETRY_MAX_SECONDS,
    SELF_CHANGE_SUMMARY_MAX_ITEMS,
)

if TYPE_CHECKING:
    from muika.core.loop import Muika

STATE_KEY = "self_change"
"""system_state 表中感知账本的键名。"""

ChangeOrigin = Literal["runtime", "boot", "command", "self"]
RegisterKind = Literal["edited", "updated", "upgraded", "downgraded"]
"""变更来源：runtime=watcher 观察的外部热重载（入账）；boot=启动对比（入账）；
command=Master 命令重载（只写记忆事实）；self=她自己的修改（只推进基线）。"""
EntryKind = Literal["kernel_file", "kernel_version", "plugin_added", "plugin_removed", "plugin_changed"]


def _entry_key(kind: str, target: str) -> str:
    return f"{kind}:{target}"


def _plugin_meta(package_name: str) -> Optional[dict]:
    """快照插件名称与描述；投递时插件可能已卸载，必须在检测时留存。"""
    plugin = get_plugins().get(package_name)
    if plugin is None or plugin.meta is None:
        return None
    return {"name": plugin.meta.name, "description": plugin.meta.description}


def _plugin_display_name(package_name: str, meta: Optional[dict]) -> str:
    if meta and meta.get("name"):
        return str(meta["name"])
    return package_name.rsplit(".", 1)[-1]


def _user_plugins() -> dict:
    """返回已加载的用户插件；builtin 插件属于内核，走内核指纹归因。"""
    return {name: plugin for name, plugin in get_plugins().items() if not is_builtin_plugin(name)}


class SelfChangeLedger:
    """待感知账本：持久化变更事实、推进观察基线、管理投递批次。

    所有读改写都在实例锁内完成；一次 ``_save`` 覆盖基线与账本，
    因此"追加变更"与"推进基线"天然处于同一事务。
    """

    def __init__(self, muika: "Muika") -> None:
        self._muika = muika
        self._lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()

    async def load_state(self) -> dict:
        """读取账本状态；无记录时返回空结构。"""
        async with self._lock:
            return await self._load_unlocked()

    async def _load_unlocked(self) -> dict:
        async with get_session(record_activity=False) as db:
            row = await db.get(SystemStateORM, STATE_KEY)
        if row is None:
            return {
                "fingerprints": {},
                "pending": [],
                "delivery": {"last_delivered_at": None, "in_flight": None, "perceived_batches": 0},
            }
        try:
            state = json.loads(row.payload)
        except ValueError:
            logger.warning("[SelfChange] Corrupted state payload -- starting a fresh ledger.")
            state = {}
        state.setdefault("fingerprints", {})
        state.setdefault("pending", [])
        delivery = state.setdefault("delivery", {})
        delivery.setdefault("last_delivered_at", None)
        delivery.setdefault("in_flight", None)
        delivery.setdefault("perceived_batches", 0)
        return state

    async def _save_unlocked(self, state: dict) -> None:
        payload = json.dumps(state, ensure_ascii=False, sort_keys=True)
        async with get_session() as db:
            row = await db.get(SystemStateORM, STATE_KEY)
            if row is None:
                db.add(SystemStateORM(key=STATE_KEY, payload=payload, updated_at=str(time.time())))
            else:
                row.payload = payload
                row.updated_at = str(time.time())

    def _next_revision(self, state: dict) -> int:
        state["revision_counter"] = state.get("revision_counter", 0) + 1
        return state["revision_counter"]

    def _merge_pending(self, state: dict, entry: dict, now: float) -> None:
        """按聚合键合并变更事实；同键再次观察推进修订号而不是夸大次数。"""
        for existing in state["pending"]:
            if existing["key"] == entry["key"]:
                existing["origins"] = sorted(set(existing["origins"]) | set(entry["origins"]))
                existing["after"] = entry["after"]
                existing["last_ts"] = entry["last_ts"]
                existing["observations"] += 1
                existing["revision"] = self._next_revision(state)
                if entry.get("version_to"):
                    existing["version_to"] = entry["version_to"]
                return
        entry["revision"] = self._next_revision(state)
        state["pending"].append(entry)

    def _new_entry(
        self, kind: str, target: str, *, origin: str, before: Optional[dict], after: Optional[dict], now: float
    ) -> dict:
        return {
            "key": _entry_key(kind, target),
            "kind": kind,
            "target": target,
            "origins": [origin],
            "before": before,
            "after": after,
            "plugin_meta": _plugin_meta(target) if kind.startswith("plugin_") else None,
            "version_from": (before or {}).get("version") if kind == "kernel_version" else None,
            "version_to": (after or {}).get("version") if kind == "kernel_version" else None,
            "first_ts": now,
            "last_ts": now,
            "observations": 1,
            "restart_id": None,
        }

    async def observe_plugin_change(self, package_name: str, origin: str, action: str) -> bool:
        """插件生命周期成功后的观察入口：基线照常推进，来源决定是否入账。

        :return: 是否观察到了真实的内容变化
        """
        now = time.time()
        plugins = get_plugins()
        digest = (
            compute_plugin_digest({package_name: plugins[package_name]}).get(package_name)
            if package_name in plugins
            else None
        )
        changed = False
        async with self._lock:
            state = await self._load_unlocked()
            user_plugins = state["fingerprints"].setdefault("user_plugins", {})
            old = user_plugins.get(package_name)
            entry = None
            if digest is None:
                if old is None:
                    return False
                del user_plugins[package_name]
                changed = True
                if origin == "runtime":
                    entry = self._new_entry(
                        "plugin_removed", package_name, origin=origin, before={"digest": old}, after=None, now=now
                    )
            elif old == digest:
                return False
            else:
                user_plugins[package_name] = digest
                changed = True
                if origin == "runtime":
                    kind = "plugin_added" if old is None else "plugin_changed"
                    entry = self._new_entry(
                        kind,
                        package_name,
                        origin=origin,
                        before={"digest": old} if old else None,
                        after={"digest": digest},
                        now=now,
                    )
            if entry is not None:
                self._merge_pending(state, entry, now)
            await self._save_unlocked(state)
        if changed and origin == "command":
            await self._note_command_reload(package_name, digest)
        return changed

    async def _note_command_reload(self, package_name: str, digest: Optional[str]) -> None:
        """命令重载她知情执行：写一条简短事实供日后想起，不入感知账本。"""
        if self._muika.memory is None:
            return
        if digest is None:
            content = f"Master unloaded my plugin {package_name}."
            source = f"plugin-reload:{package_name}:removed"
        else:
            content = f"Master reloaded my plugin {package_name}; its code changed."
            source = f"plugin-reload:{package_name}:{digest[:8]}"
        await self._muika.memory.add_material("note", content, source=source)

    async def observe_kernel_boot(self, *, restart_id: Optional[str], self_paths: set[str]) -> list[dict]:
        """启动对比：磁盘指纹 vs 已观察基线；外部变更入账，基线无条件推进。"""
        now = time.time()
        kernel = compute_kernel_files()
        plugin_digests = compute_plugin_digest(_user_plugins())
        version = get_version()
        async with self._lock:
            state = await self._load_unlocked()
            old_fp = state["fingerprints"]
            entries: list[dict] = []
            if old_fp:
                entries.extend(self._diff_kernel(old_fp.get("kernel", {}), kernel, self_paths, now))
                if old_fp.get("version"):
                    entries.extend(self._diff_version(old_fp.get("version", ""), version, now))
                entries.extend(self._diff_plugins(old_fp.get("user_plugins", {}), plugin_digests, now))
            for entry in entries:
                entry["restart_id"] = restart_id
                self._merge_pending(state, entry, now)
            state["fingerprints"] = {"version": version, "kernel": kernel, "user_plugins": plugin_digests}
            await self._save_unlocked(state)
        return entries

    def _diff_kernel(self, old_kernel: dict, kernel: dict, self_paths: set[str], now: float) -> list[dict]:
        entries = []
        for rel in sorted(set(old_kernel) | set(kernel)):
            before, after = old_kernel.get(rel), kernel.get(rel)
            if before == after or rel in self_paths:
                continue
            entries.append(
                self._new_entry(
                    "kernel_file",
                    rel,
                    origin="boot",
                    before={"digest": before} if before else None,
                    after={"digest": after} if after else None,
                    now=now,
                )
            )
        return entries

    def _diff_version(self, old_version: str, version: str, now: float) -> list[dict]:
        if old_version == version or "Unknown" in {old_version, version}:
            return []
        return [
            self._new_entry(
                "kernel_version",
                "version",
                origin="boot",
                before={"version": old_version},
                after={"version": version},
                now=now,
            )
        ]

    def _diff_plugins(self, old_plugins: dict, plugin_digests: dict, now: float) -> list[dict]:
        entries = []
        for package_name in sorted(set(old_plugins) | set(plugin_digests)):
            old_digest, new_digest = old_plugins.get(package_name), plugin_digests.get(package_name)
            if old_digest == new_digest:
                continue
            if new_digest is None:
                kind, before, after = "plugin_removed", {"digest": old_digest}, None
            elif old_digest is None:
                kind, before, after = "plugin_added", None, {"digest": new_digest}
            else:
                kind, before, after = "plugin_changed", {"digest": old_digest}, {"digest": new_digest}
            entries.append(self._new_entry(kind, package_name, origin="boot", before=before, after=after, now=now))
        return entries

    async def freeze_batch(self) -> Optional[tuple[str, list[dict]]]:
        """冻结当前账本为一次投递批次；期间新到的变化留在账本之外。"""
        async with self._lock:
            state = await self._load_unlocked()
            if state["delivery"].get("in_flight") or not state["pending"]:
                return None
            batch_id = uuid.uuid4().hex
            snapshot = [dict(entry) for entry in state["pending"]]
            state["delivery"]["in_flight"] = {
                "batch_id": batch_id,
                "snapshot": snapshot,
                "created_at": time.time(),
                "attempts": 1,
                # 首次投递结果未知前，竞态检查不得把同一批次当"到期重投"
                "next_attempt_at": time.time() + SELF_CHANGE_RETRY_BASE_SECONDS,
                "awaiting_flush": False,
            }
            await self._save_unlocked(state)
        return batch_id, snapshot

    async def mark_dispatched(self, batch_id: str) -> None:
        """事件已入队：退出重投资格，等待 on_processed 的确认或改期。

        若处理时间超过批次超时（如 LLM 长生成），宁可等超时重组也不重复入队。
        """
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if not in_flight or in_flight["batch_id"] != batch_id:
                return
            in_flight["next_attempt_at"] = None
            await self._save_unlocked(state)

    async def park_batch(self, batch_id: str) -> None:
        """回执为 queued：消息在暂存队列等待连接恢复，停用重投，待适配器上线后确认。

        这样既不在账本侧重试（避免与暂存队列补发重复），也不会在进程退出前丢账。
        """
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if not in_flight or in_flight["batch_id"] != batch_id:
                return
            in_flight["awaiting_flush"] = True
            in_flight["next_attempt_at"] = None
            await self._save_unlocked(state)

    async def discard_stale_batch(self) -> None:
        """在途批次超时视为投递失败：账目未销，重组后自然重投。

        等待暂存补发（awaiting_flush）的批次不受超时约束：重投会造成重复发言。
        """
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if (
                in_flight
                and not in_flight.get("awaiting_flush")
                and time.time() - in_flight["created_at"] > SELF_CHANGE_BATCH_MAX_AGE
            ):
                logger.warning(f"[SelfChange] Batch {in_flight['batch_id'][:8]} timed out in flight; will re-dispatch.")
                state["delivery"]["in_flight"] = None
                await self._save_unlocked(state)

    async def get_batch_snapshot(self, batch_id: str) -> Optional[list[dict]]:
        """返回在途批次的快照；批次不存在或已销账时返回 None。"""
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if not in_flight or in_flight["batch_id"] != batch_id:
                return None
            return in_flight["snapshot"]

    async def resolve_batch(self, batch_id: str) -> None:
        """销账：只消费快照内的记录修订，快照之后的新变化原地保留。

        记忆写入由调用方在销账**之前**完成（幂等 source）——账本清账后
        记忆若缺失将无法补写，顺序不可颠倒。
        """
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if not in_flight or in_flight["batch_id"] != batch_id:
                return
            snapshot = in_flight["snapshot"]
            for entry in snapshot:
                for index, pending_entry in enumerate(state["pending"]):
                    if pending_entry["key"] == entry["key"]:
                        if pending_entry["revision"] == entry["revision"]:
                            del state["pending"][index]
                        break
            state["delivery"]["in_flight"] = None
            state["delivery"]["last_delivered_at"] = time.time()
            state["delivery"]["perceived_batches"] += 1
            await self._save_unlocked(state)

    async def defer_batch(self, batch_id: str) -> None:
        """投递失败：保留在途批次，累积重试次数并安排退避后的重投时刻。"""
        async with self._lock:
            state = await self._load_unlocked()
            in_flight = state["delivery"].get("in_flight")
            if not in_flight or in_flight["batch_id"] != batch_id:
                return
            in_flight["attempts"] += 1
            in_flight["next_attempt_at"] = time.time() + min(
                SELF_CHANGE_RETRY_MAX_SECONDS, SELF_CHANGE_RETRY_BASE_SECONDS * (2 ** (in_flight["attempts"] - 1))
            )
            await self._save_unlocked(state)


def describe_entries(entries: list[dict]) -> str:
    """把账本条目压缩为一段可供记忆留存的事实描述。"""
    return "; ".join(_entry_facts(entry) for entry in entries)


def _entry_facts(entry: dict) -> str:
    kind, target = entry["kind"], entry["target"]
    if kind == "kernel_file":
        return f"core file {target} changed"
    if kind == "kernel_version":
        return f"version changed {entry.get('version_from')} -> {entry.get('version_to')}"
    name = _plugin_display_name(target, entry.get("plugin_meta"))
    if kind == "plugin_added":
        return f'plugin "{name}" appeared'
    if kind == "plugin_removed":
        return f'plugin "{name}" was removed'
    return f'plugin "{name}" was modified'


def _fold(items: list[str]) -> str:
    if len(items) <= SELF_CHANGE_SUMMARY_MAX_ITEMS:
        return ", ".join(items)
    hidden = len(items) - SELF_CHANGE_SUMMARY_MAX_ITEMS
    return ", ".join(items[:SELF_CHANGE_SUMMARY_MAX_ITEMS]) + f" (+{hidden} more)"


def _build_report(snapshot: list[dict]) -> tuple[str, RegisterKind, Optional[str], Optional[str]]:
    """组装 [System] 事实行并判定语域。"""
    version_entry = next((entry for entry in snapshot if entry["kind"] == "kernel_version"), None)
    version_from = version_entry.get("version_from") if version_entry else None
    version_to = version_entry.get("version_to") if version_entry else None
    has_runtime = any("runtime" in entry["origins"] for entry in snapshot)

    parts: list[str] = []
    if version_entry:
        parts.append(f"Your version changed: {version_from} -> {version_to}.")
    kernel_files = [entry["target"] for entry in snapshot if entry["kind"] == "kernel_file"]
    if kernel_files:
        parts.append("Core files changed: " + _fold(kernel_files) + ".")
    for entry in snapshot:
        if entry["kind"].startswith("plugin_"):
            parts.append(_plugin_facts(entry))
    if has_runtime and not version_entry:
        parts.append("The changes happened while you were running.")
    if version_entry:
        comparison = compare_versions(version_from or "", version_to or "")
        register: RegisterKind = "updated"
        if comparison is not None and comparison < 0:
            register = "upgraded"
        elif comparison is not None and comparison > 0:
            register = "downgraded"
    elif has_runtime:
        register = "edited"
    else:
        register = "updated"
    return " ".join(parts), register, version_from, version_to


def _plugin_facts(entry: dict) -> str:
    kind, target = entry["kind"], entry["target"]
    meta = entry.get("plugin_meta") or {}
    name = _plugin_display_name(target, meta)
    description = (meta.get("description") or "").strip()
    suffix = f": {description}" if description and kind == "plugin_added" else ""
    if kind == "plugin_added":
        return f'A new plugin "{name}" appeared{suffix}.'
    if kind == "plugin_removed":
        return f'Your plugin "{name}" was removed.'
    return f'Your plugin "{name}" was modified{suffix}.'


class SelfChangeDispatcher:
    """投递调度器：软条件（沉降、间隔）决定何时说，硬条件决定能否说。"""

    def __init__(self, muika: "Muika", ledger: SelfChangeLedger, can_send: Callable[[], bool]) -> None:
        self._muika = muika
        self.ledger = ledger
        self._can_send = can_send
        self._timer: Optional[asyncio.Task] = None
        self._background: set[asyncio.Task] = set()
        self._check_lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()

    def wake(self, delay: float = 0.0) -> None:
        """（重）安排一次门控检查；幂等，可由任意唤醒源调用。"""
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.loop:
            self._cancel_timer()
            self._timer = asyncio.create_task(self._check_after(max(delay, 0.0)))
        else:
            self.loop.call_soon_threadsafe(self.wake, delay)

    def shutdown(self) -> None:
        """取消挂起的检查；账本与单例保留，等待下次唤醒。"""
        self._cancel_timer()

    async def aclose(self) -> None:
        """完全停机：取消定时器与所有后台任务，确保 close_db 前无在途 DB 操作。"""
        self.shutdown()
        tasks = [task for task in self._background if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._background.clear()

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def _check_after(self, delay: float) -> None:
        task = asyncio.current_task()
        try:
            if delay:
                await asyncio.sleep(delay)
            await self.check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[SelfChange] Delivery check failed.")
        finally:
            # 仅当仍指向本任务时清空；否则 _timer 属于更新的唤醒
            if self._timer is task:
                self._timer = None

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)

        def _finish(done: asyncio.Task) -> None:
            self._background.discard(done)
            if not done.cancelled() and (error := done.exception()) is not None:
                logger.error(f"[SelfChange] Background task failed: {error}")

        task.add_done_callback(_finish)

    async def check(self) -> None:
        """门控检查：条件满足时冻结账本并投递一次合并的感知事件。"""
        if not mas_config.self_change_awareness_enabled or not self._muika.is_alive:
            return
        async with self._check_lock:
            await self._check_locked()

    async def _check_locked(self) -> None:
        await self.ledger.discard_stale_batch()
        state = await self.ledger.load_state()
        if not state["pending"]:
            return
        now = time.time()
        # 硬条件前置：新投递与重投都必须能真的发出去
        if not (self._can_send() and not is_core_maintenance_active()):
            self.wake(SELF_CHANGE_RETRY_BASE_SECONDS)
            return
        in_flight = state["delivery"].get("in_flight")
        if in_flight:
            # 已有在途批次：仅到点的重投才重发同一快照；等待处理/暂存补发时不动作
            next_attempt_at = in_flight.get("next_attempt_at")
            if next_attempt_at is None or in_flight.get("awaiting_flush"):
                return
            if now >= next_attempt_at:
                await self._dispatch(in_flight["batch_id"], in_flight["snapshot"], state)
            else:
                self.wake(next_attempt_at - now)
            return
        settle = mas_config.self_change_settle_seconds
        since_append = now - max(entry["last_ts"] for entry in state["pending"])
        if since_append < settle:
            self.wake(settle - since_append)
            return
        last_delivered = state["delivery"].get("last_delivered_at") or 0.0
        overdue = (
            now - min(entry["first_ts"] for entry in state["pending"])
        ) >= mas_config.self_change_max_defer_seconds
        if not overdue and (now - last_delivered) < mas_config.self_change_min_interval_seconds:
            self.wake(mas_config.self_change_min_interval_seconds - (now - last_delivered))
            return
        frozen = await self.ledger.freeze_batch()
        if frozen is None:
            return
        batch_id, snapshot = frozen
        await self._dispatch(batch_id, snapshot, state)

    async def _dispatch(self, batch_id: str, snapshot: list[dict], state: dict) -> None:
        """把（新冻结或重试的）批次作为一次感知事件交给她。"""
        report, register, version_from, version_to = _build_report(snapshot)
        payload = SelfChangedPayload(
            batch_id=batch_id,
            register=register,
            report=report,
            version_from=version_from,
            version_to=version_to,
            times_noticed=state["delivery"]["perceived_batches"],
        )
        logger.info(f"[SelfChange] Dispatching batch {batch_id[:8]}: {len(snapshot)} change(s), register={register}.")
        logger.debug(f"[SelfChange] Batch {batch_id[:8]} report: {report}")
        await self._muika.create_event(SelfChangedEvent(payload=payload))
        # 事件已入队：退出重投资格，避免处理期间的后续变更把同一批次重复入队
        await self.ledger.mark_dispatched(batch_id)

    @staticmethod
    def _retry_delay(attempts: int) -> float:
        return min(SELF_CHANGE_RETRY_MAX_SECONDS, SELF_CHANGE_RETRY_BASE_SECONDS * (2 ** (attempts - 1)))

    def on_processed(self, batch_id: str, *, silent: bool, reply: str, receipt) -> None:
        """认知管线处理完感知事件后的确认回调：区分沉默、兜底与传输回执。"""
        self._spawn(self._finalize(batch_id, silent=silent, reply=reply, receipt=receipt))

    async def _finalize(self, batch_id: str, *, silent: bool, reply: str, receipt) -> None:
        if receipt is SendReceipt.QUEUED:
            # 消息已进入暂存队列等连接恢复：停用重投以免与补发重复，
            # 适配器上线补发完成后再销账
            await self.ledger.park_batch(batch_id)
            logger.info(f"[SelfChange] Batch {batch_id[:8]} queued for staging; awaiting adapter flush.")
            return
        if not (silent or reply) or reply == FALLBACK_REPLY or receipt is SendReceipt.FAILED:
            await self.ledger.defer_batch(batch_id)
            state = await self.ledger.load_state()
            in_flight = state["delivery"].get("in_flight")
            attempts = in_flight["attempts"] if in_flight else 1
            delay = self._retry_delay(attempts)
            logger.warning(f"[SelfChange] Batch {batch_id[:8]} not delivered; retrying in {delay:.2f}s.")
            self.wake(delay)
            return
        await self._confirm(batch_id)

    def on_adapter_online(self) -> None:
        """适配器上线：暂存队列即将补发，确认此前因 queued 停靠的批次。

        以后台任务执行——主循环任务可能随时被 gateway 待机取消，
        在循环任务内做 DB 操作会在取消时污染共享连接。
        """
        self._spawn(self._confirm_parked())

    async def _confirm_parked(self) -> None:
        state = await self.ledger.load_state()
        in_flight = state["delivery"].get("in_flight")
        if in_flight and in_flight.get("awaiting_flush"):
            await self._confirm(in_flight["batch_id"])

    async def _confirm(self, batch_id: str) -> None:
        """确认感知：先幂等写入长期记忆，成功后才销账——顺序不可颠倒。"""
        snapshot = await self.ledger.get_batch_snapshot(batch_id)
        if snapshot is not None:
            try:
                await self._muika.memory.add_material(
                    "agent", describe_entries(snapshot), source=f"self_change:{batch_id}"
                )
            except Exception:
                logger.exception(f"[SelfChange] Memory write failed for batch {batch_id[:8]}; deferring.")
                await self.ledger.defer_batch(batch_id)
                state = await self.ledger.load_state()
                in_flight = state["delivery"].get("in_flight")
                attempts = in_flight["attempts"] if in_flight else 1
                self.wake(self._retry_delay(attempts))
                return
        await self.ledger.resolve_batch(batch_id)
        logger.info(f"[SelfChange] Batch {batch_id[:8]} confirmed.")


_ledger: Optional[SelfChangeLedger] = None
_dispatcher: Optional[SelfChangeDispatcher] = None


def setup_self_change(muika: "Muika", can_send: Callable[[], bool]) -> None:
    """构造账本与调度器，接线插件层观察者并恢复既有账目的调度。"""
    global _ledger, _dispatcher
    _ledger = SelfChangeLedger(muika)
    _dispatcher = SelfChangeDispatcher(muika, _ledger, can_send)
    muika.self_change = _dispatcher
    from muika.plugin.manager import set_plugin_change_observer

    set_plugin_change_observer(notify_plugin_change)
    _dispatcher.wake()


def teardown_self_change() -> None:
    """解除接线并丢弃单例；进程退出或多 CoreApp 测试轮换时调用。"""
    global _ledger, _dispatcher
    from muika.plugin.manager import set_plugin_change_observer

    set_plugin_change_observer(None)
    if _dispatcher is not None:
        _dispatcher.shutdown()
    _ledger = None
    _dispatcher = None


async def aclose_self_change() -> None:
    """完全停机并等待后台任务退出；必须在 ``close_db`` 之前调用，
    避免在途 DB 操作在引擎销毁后报"no active connection"。
    """
    if _dispatcher is not None:
        await _dispatcher.aclose()
    teardown_self_change()


def get_self_change_dispatcher() -> Optional[SelfChangeDispatcher]:
    """返回当前调度器单例；未接线时为 None。"""
    return _dispatcher


def notify_plugin_change(package_name: str, origin: str, action: str) -> None:
    """插件生命周期成功后的同步观察入口；供 muika.plugin 层调用。

    内部把账本写入调度回当前事件循环，跨线程调用同样安全；
    事件循环已关闭（如测试收尾）时静默丢弃。
    """
    dispatcher, ledger = _dispatcher, _ledger
    if dispatcher is None or ledger is None or dispatcher.loop.is_closed():
        return

    def _schedule() -> None:
        dispatcher._spawn(_observe(ledger, dispatcher, package_name, origin, action))

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is dispatcher.loop:
        _schedule()
    else:
        try:
            dispatcher.loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            logger.debug("[SelfChange] Event loop closed; dropping plugin change observation.")


async def _observe(
    ledger: "SelfChangeLedger", dispatcher: "SelfChangeDispatcher", package_name: str, origin: str, action: str
) -> None:
    try:
        await ledger.observe_plugin_change(package_name, origin, action)
    except Exception:
        logger.exception(f"[SelfChange] Could not observe plugin change for {package_name!r}.")
        return
    dispatcher.wake()


def _proposal_self_paths(restart_record: Optional[Mapping[str, Any]]) -> tuple[set[str], Optional[str]]:
    """从重启记录关联的提案中确认本次自改实际落地的内核文件集合。

    只有本次重启（status=started）且**当前文件内容与提案预期结果一致**的路径
    才豁免入账——旧的自改记录、被玩家再次修改的文件，都不能屏蔽外部变更。
    提案读不到时保守返回空集合：宁可让已知的自改走一遍账本，
    也不把并存的外部修改整批吞掉。
    """
    if not restart_record or not restart_record.get("patch_id") or restart_record.get("status") != "started":
        return set(), None
    restart_id = restart_record.get("id")
    proposal_file = restart_record.get("proposal_file")
    if not proposal_file:
        return set(), restart_id
    try:
        proposal = json.loads(Path(proposal_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("[SelfChange] Restart proposal file unreadable; kernel diff stays external.")
        return set(), restart_id
    package_name = kernel_root().name
    paths: set[str] = set()
    for change in proposal.get("changes", []):
        parts = PurePosixPath(str(change.get("path", ""))).parts
        if not parts or parts[0] != package_name:
            continue
        rel = PurePosixPath(*parts[1:]).as_posix()
        if not _self_change_landed(kernel_root() / rel, change):
            continue
        paths.add(rel)
    return paths, restart_id


def _self_change_landed(path: Path, change: Mapping[str, Any]) -> bool:
    """核对提案变更是否就是磁盘上的现状（摘要语义与提案体系一致：utf-8 文本）。"""
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except (OSError, ValueError):
        return False
    expected = change.get("sha256_after")
    if expected is None:
        # delete 变更：文件确已消失才算落地
        return text is None
    if text is None:
        return False
    return hashlib.sha256(text.encode("utf-8")).hexdigest() == expected


async def run_boot_self_change_check(muika: "Muika", restart_record: Optional[Mapping[str, Any]]) -> None:
    """启动时对比运行指纹与已观察基线：外部变更入账，并恢复投递调度。

    感知不应挡住她的启动：检测失败只记录日志，本次启动放弃 diff。
    :param restart_record: 监督进程保存的重启记录；独立运行时为 None
    """
    if _ledger is None or _dispatcher is None:
        logger.debug("[SelfChange] Not wired -- skipping boot check.")
        return
    try:
        self_paths, restart_id = _proposal_self_paths(restart_record)
        entries = await _ledger.observe_kernel_boot(restart_id=restart_id, self_paths=self_paths)
    except Exception:
        logger.exception("[SelfChange] Boot fingerprint check failed; skipping this boot's diff.")
        return
    if entries:
        logger.info(f"[SelfChange] Boot diff observed {len(entries)} external change(s).")
    _dispatcher.wake()
