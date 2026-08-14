"""自我修改管理器：校验、备份、原子写入与审计的统一入口。"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from muika.config import mas_config
from muika.database.crud import SelfModificationCRUD
from muika.database.db import get_session
from muika.utils.logger import logger

from .policy import SelfModError, display_path, infer_layer, resolve_self_path
from .validators import validate_content


class SelfModManager:
    """所有自我修改的统一通道，保证"校验 → 备份 → 原子写 → 审计"顺序。"""

    def __init__(self) -> None:
        self._backup_dir = Path(mas_config.self_mod_backup_dir)

    # ------------------------------------------------------------------
    # 公共 API
    # ------------------------------------------------------------------

    async def apply(
        self,
        raw_path: str,
        content: str,
        reason: str,
        source: str = "self",
    ) -> str:
        """应用一次自我修改，返回供 LLM 阅读的结果报告。

        :param raw_path: 目标文件路径（沙箱内相对或绝对路径）
        :param content: 完整的新文件内容（全量覆写）
        :param reason: 修改动机，必填且写入审计日志
        :param source: 触发来源标记（self / command 等）
        :raises SelfModError: 策略或校验拒绝时抛出
        """
        resolved = resolve_self_path(raw_path, require_write=True)
        rel = display_path(resolved)
        layer = infer_layer(resolved)

        validate_content(resolved, content)

        before_path_rel: Optional[str] = None
        if resolved.exists():
            if not resolved.is_file():
                raise SelfModError(f"Target is not a file: {resolved}")
            before_text = resolved.read_text(encoding="utf-8", errors="replace")
            before_path_rel = self._write_snapshot(resolved, before_text, suffix="")

        self._atomic_write(resolved, content)
        after_path_rel = self._write_snapshot(resolved, content, suffix=".after")

        revision_id = await self._audit(
            layer=layer,
            path=rel,
            action="write",
            reason=reason,
            before_path=before_path_rel,
            after_path=after_path_rel,
            source=source,
        )

        logger.info(f"[SelfMod] {rel} updated by {source} (revision #{revision_id}): {reason[:80]}")
        return (
            f"Self-modification applied (revision #{revision_id}): {rel}\n"
            f"Layer: {layer}. The change takes effect immediately.\n"
            f"Reason recorded: {reason}\n"
            f"Use self_revert(path={rel!r}) if you want to undo this."
        )

    async def revert(
        self,
        raw_path: str,
        revision_id: Optional[int] = None,
    ) -> str:
        """将文件回滚到指定版本（默认上一版本），返回结果报告。

        :param raw_path: 目标文件路径
        :param revision_id: 审计记录 id；为 None 时回滚到最近一次修改之前
        """
        resolved = resolve_self_path(raw_path, require_write=True)
        rel = display_path(resolved)
        layer = infer_layer(resolved)

        async with get_session() as session:
            if revision_id is not None:
                record = await SelfModificationCRUD.get_by_id(session, revision_id)
                if record is None or record.path != rel:
                    raise SelfModError(f"Revision #{revision_id} not found for {rel}.")
            else:
                record = await SelfModificationCRUD.latest_write_for_path(session, rel)
                if record is None:
                    raise SelfModError(f"No applied self-modification for {rel}; nothing to revert.")

            before_path_rel: Optional[str] = record.before_path
            target_id = record.id
            record.status = "reverted"  # 已回滚的写入不再作为缺省 revert 目标

        current_text: Optional[str] = None
        if resolved.exists():
            current_text = resolved.read_text(encoding="utf-8", errors="replace")

        if before_path_rel is None:
            # 该版本之前文件不存在——回滚即删除
            if resolved.exists():
                resolved.unlink()
            report_action = "deleted (restored to built-in / non-existent state)"
            after_path_rel = None
        else:
            target_text = self._read_snapshot(before_path_rel)
            validate_content(resolved, target_text)
            self._atomic_write(resolved, target_text)
            after_path_rel = self._write_snapshot(resolved, target_text, suffix=".after")
            report_action = f"restored to the state before revision #{target_id}"

        await self._audit(
            layer=layer,
            path=rel,
            action="rollback",
            reason=f"Reverted {rel} (target revision #{target_id})",
            before_path=(
                self._write_snapshot_text(rel, current_text, suffix=".pre_revert") if current_text is not None else None
            ),
            after_path=after_path_rel,
            source="self",
        )
        logger.info(f"[SelfMod] {rel} reverted (target revision #{target_id})")
        return f"Reverted {rel}: {report_action}."

    async def history(self, raw_path: str = "", limit: int = 15) -> str:
        """返回审计日志摘要文本。"""
        path_filter: Optional[str] = None
        if raw_path:
            # 仅用于审计查询（只读）
            resolved = resolve_self_path(raw_path)
            path_filter = display_path(resolved)

        async with get_session() as session:
            records = await SelfModificationCRUD.list_recent(session, path=path_filter, limit=limit)

        if not records:
            return "No self-modifications recorded yet."

        lines = ["Self-modification history (newest first):"]
        for r in records:
            ts = r.created_at[:19].replace("T", " ")
            lines.append(f"  #{r.id} [{ts}] {r.action} ({r.layer}) {r.path} — {r.reason[:60]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _write_snapshot(self, resolved: Path, text: str, suffix: str) -> str:
        """将快照写入备份目录，返回相对备份目录的文件名。"""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        escaped = display_path(resolved).replace("/", "_")
        filename = f"{ts}__{escaped}{suffix}"
        (self._backup_dir / filename).write_text(text, encoding="utf-8")
        return filename

    def _write_snapshot_text(self, rel_path: str, text: Optional[str], suffix: str) -> Optional[str]:
        """将文本快照写入备份目录（按相对路径命名），返回文件名；text 为 None 返回 None。"""
        if text is None:
            return None
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        escaped = rel_path.replace("/", "_")
        filename = f"{ts}__{escaped}{suffix}"
        (self._backup_dir / filename).write_text(text, encoding="utf-8")
        return filename

    def _read_snapshot(self, filename: str) -> str:
        """从备份目录读取快照内容。"""
        path = self._backup_dir / filename
        return path.read_text(encoding="utf-8", errors="replace")

    def _find_before_snapshots(self, rel_path: str) -> list[Path]:
        """按时间顺序返回指定相对路径的全部 before 快照（不含 .after 后缀）。"""
        if not self._backup_dir.exists():
            return []
        escaped = rel_path.replace("/", "_")
        return sorted(
            p
            for p in self._backup_dir.glob(f"*__{escaped}")
            if not p.name.endswith(".after") and not p.name.endswith(".pre_revert")
        )

    @staticmethod
    def _atomic_write(resolved: Path, content: str) -> None:
        """先写临时文件再原子替换，避免半写状态。"""
        resolved.parent.mkdir(parents=True, exist_ok=True)
        tmp = resolved.parent / (resolved.name + ".selfmod.tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, resolved)

    @staticmethod
    async def _audit(
        *,
        layer: str,
        path: str,
        action: str,
        reason: str,
        before_path: Optional[str],
        after_path: Optional[str],
        source: str,
    ) -> int:
        """写入一条审计记录并返回其 id；DB 不可用时降级为仅日志。"""
        try:
            async with get_session() as session:
                record = await SelfModificationCRUD.create(
                    session,
                    layer=layer,
                    path=path,
                    action=action,
                    reason=reason,
                    before_path=before_path,
                    after_path=after_path,
                    source=source,
                )
                await session.flush()
                return record.id
        except Exception as e:
            logger.error(f"[SelfMod] Audit write failed (change still applied): {e}")
            return -1


_manager: Optional[SelfModManager] = None


def get_self_mod_manager() -> SelfModManager:
    """获取 SelfModManager 单例。"""
    global _manager
    if _manager is None:
        _manager = SelfModManager()
    return _manager
