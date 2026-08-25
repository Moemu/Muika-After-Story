"""单文件插件的验证、部署、隔离和恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from muika.config import mas_config
from muika.plugin.command import _commands
from muika.plugin.func_call.caller import _caller_data
from muika.plugin.loader import (
    get_plugins,
    try_load_plugin,
    try_reload_plugin,
    unload_plugin,
)
from muika.plugin.manager import get_plugin_manager
from muika.utils.logger import logger

from .manager import get_self_mod_manager
from .policy import SelfModError, display_path, resolve_self_path
from .validators import validate_content

_PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PROBE_PREFIX = "MUIKA_PLUGIN_PROBE="
_PROBE_TIMEOUT = 10.0
_OUTPUT_LIMIT = 8 * 1024
_deploy_lock = asyncio.Lock()


@dataclass
class QuarantineRecord:
    """隔离项元数据。"""

    quarantine_id: str
    target_path: str
    module_name: str
    created_at: str
    candidate_sha256: str
    expected_base_sha256: Optional[str]
    error: str
    restore_attempts: int = 0


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> Optional[str]:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PluginDeployer:
    """管理 Muika 自写单文件插件。"""

    def __init__(self) -> None:
        self._plugins_dir = Path(mas_config.plugins_dir).resolve()
        self._staging_dir = self._plugins_dir / "_staging"
        self._quarantine_dir = self._plugins_dir / "_quarantine"

    def resolve_plugin_path(self, raw_path: str) -> tuple[Path, str]:
        """校验插件路径，并返回正式路径和模块名。"""
        if not mas_config.enable_self_modification:
            raise SelfModError("Self-modification is disabled by configuration.")
        if not mas_config.enable_plugin_self_modification:
            raise SelfModError("Plugin self-modification is disabled by configuration.")

        resolved = resolve_self_path(raw_path, require_write=True)
        if resolved.parent != self._plugins_dir or resolved.suffix != ".py":
            raise SelfModError("Only direct plugins/<name>.py files are supported.")
        if resolved.name == "__init__.py" or not _PLUGIN_NAME_RE.fullmatch(resolved.stem):
            raise SelfModError("Plugin names must match ^[a-z][a-z0-9_]{0,63}$.")
        if (self._plugins_dir / resolved.stem).exists():
            raise SelfModError(f"A plugin package already uses the name {resolved.stem!r}.")
        return resolved, f"{self._plugins_dir.name}.{resolved.stem}"

    async def run_probe(self, module_name: str) -> tuple[bool, str]:
        """在子进程中执行完整 load 和 unload。"""
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "muika.plugin.probe",
            module_name,
            cwd=Path.cwd(),
            env=self._probe_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output = await asyncio.wait_for(self._read_probe_output(process), timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return False, f"Plugin probe timed out after {_PROBE_TIMEOUT:g} seconds."

        text = output.decode("utf-8", errors="replace")
        payload = None
        for line in reversed(text.splitlines()):
            if line.startswith(_PROBE_PREFIX):
                try:
                    payload = json.loads(line[len(_PROBE_PREFIX) :])
                except json.JSONDecodeError:
                    pass
                break
        if payload is None:
            return False, f"Plugin probe returned no result. Output:\n{text}"
        message = str(payload.get("message", "Unknown probe result"))
        if process.returncode != 0 or not payload.get("success"):
            return False, message
        return True, message

    async def deploy(self, path: str, content: str, reason: str, source: str = "self") -> str:
        """验证并部署单文件插件。"""
        async with _deploy_lock:
            return await self._deploy(path, content, reason, source)

    async def deploy_new(self, path: str, content: str, reason: str, source: str = "self") -> str:
        """仅在正式插件仍不存在时部署。"""
        async with _deploy_lock:
            target, _ = self.resolve_plugin_path(path)
            if target.exists():
                raise SelfModError(f"The new plugin target already exists: {display_path(target)}")
            return await self._deploy(path, content, reason, source)

    async def deploy_if_unchanged(
        self,
        path: str,
        content: str,
        reason: str,
        expected_sha256: str,
        source: str = "self",
    ) -> str:
        """仅在正式插件仍匹配预览哈希时部署。"""
        async with _deploy_lock:
            target, _ = self.resolve_plugin_path(path)
            if _sha256_file(target) != expected_sha256:
                raise SelfModError("The plugin changed after the preview. Create a new preview.")
            return await self._deploy(path, content, reason, source)

    async def _deploy(self, path: str, content: str, reason: str, source: str) -> str:
        """在调用方持有部署锁时执行部署。"""
        target, module_name = self.resolve_plugin_path(path)
        validate_content(target, content)
        base_hash = _sha256_file(target)
        self._atomic_write(self._staging_dir / target.name, content)
        staging_module = f"{self._plugins_dir.name}._staging.{target.stem}"
        probe_ok, probe_message = await self.run_probe(staging_module)
        if not probe_ok:
            quarantine_id = self._quarantine_candidate(target, module_name, content, base_hash, probe_message)
            raise SelfModError(f"Plugin validation failed: {probe_message} Candidate quarantined as {quarantine_id}.")

        if _sha256_file(target) != base_hash:
            self._remove_staging(target.name)
            raise SelfModError("The plugin changed during validation. Create a new preview and try again.")

        manager = get_plugin_manager()
        manager.suppress_watcher(module_name)
        self._ensure_plugins_import_path()
        was_loaded = module_name in get_plugins()
        await get_self_mod_manager().apply(str(target), content, reason, source=source)
        result = try_reload_plugin(module_name) if was_loaded else try_load_plugin(module_name)
        if result.success:
            manager.refresh_butler()
            self._remove_staging(target.name)
            commands, tools = self._owned_names(module_name)
            return (
                f"Plugin deployed: {display_path(target)}\n"
                f"Commands: {', '.join(commands) if commands else '(none)'}\n"
                f"Function tools: {', '.join(tools) if tools else '(none)'}"
            )

        activation_error = result.error or "Unknown activation error"
        quarantine_id = self._quarantine_candidate(target, module_name, content, base_hash, activation_error)
        recovery = await self._recover_failed_activation(target, module_name, was_loaded)
        if not recovery.startswith("Recovery succeeded"):
            logger.critical(
                f"[PluginDeployer] activation and recovery failed for {module_name}: " f"{activation_error}; {recovery}"
            )
        raise SelfModError(
            f"Plugin activation failed: {activation_error}\n" f"Candidate quarantined as {quarantine_id}.\n{recovery}"
        )

    async def revert(self, path: str, revision_id: Optional[int] = None) -> str:
        """安全回滚一个插件。"""
        async with _deploy_lock:
            target, module_name = self.resolve_plugin_path(path)
            self_mod_manager = get_self_mod_manager()
            old_content = target.read_text(encoding="utf-8") if target.is_file() else None
            revert_content = await self_mod_manager.read_revert_target(str(target), revision_id)
            if revert_content is not None:
                validate_content(target, revert_content)
                self._atomic_write(self._staging_dir / target.name, revert_content)
                success, message = await self.run_probe(f"{self._plugins_dir.name}._staging.{target.stem}")
                self._remove_staging(target.name)
                if not success:
                    raise SelfModError(f"The revert target failed plugin validation: {message}")

            manager = get_plugin_manager()
            manager.suppress_watcher(module_name)
            was_loaded = module_name in get_plugins()
            report = await self_mod_manager.revert(str(target), revision_id=revision_id)
            if target.exists():
                result = try_reload_plugin(module_name)
                if not result.success:
                    recovery = await self._recover_failed_revert(target, module_name, old_content, was_loaded)
                    raise SelfModError(f"Plugin revert reload failed: {result.error}\n{recovery}")
            elif module_name in get_plugins():
                unload_plugin(module_name)
            manager.refresh_butler()
            return report

    def list_quarantine(self) -> str:
        """列出隔离区内容。"""
        records = self._read_records()
        if not records:
            return "Plugin quarantine is empty."
        lines = ["Plugin quarantine:"]
        for record in records:
            lines.append(
                f"  {record.quarantine_id}  {record.module_name}  " f"{record.created_at[:19]}  {record.error[:100]}"
            )
        return "\n".join(lines)

    async def restore_quarantine(self, quarantine_id: str) -> str:
        """重新验证并恢复一个隔离项。"""
        async with _deploy_lock:
            record, metadata_path = self._get_record(quarantine_id)
            candidate_path = metadata_path.with_suffix(".py")
            current_hash = _sha256_file(Path(record.target_path))
            if current_hash != record.expected_base_sha256:
                raise SelfModError("The target plugin changed after quarantine. Restore is refused.")

            record.restore_attempts += 1
            metadata_path.write_text(json.dumps(asdict(record), ensure_ascii=False, indent=2), encoding="utf-8")
            content = candidate_path.read_text(encoding="utf-8")
            report = await self._deploy(
                record.target_path,
                content,
                f"Restore quarantine {quarantine_id}",
                source="restore",
            )
            candidate_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            await get_self_mod_manager().record_event(
                record.target_path,
                action="restore",
                reason=f"Restored quarantine {quarantine_id}",
                source="restore",
            )
            return report

    async def _recover_failed_activation(self, target: Path, module_name: str, was_loaded: bool) -> str:
        try:
            await get_self_mod_manager().revert(str(target))
            if target.exists():
                result = try_load_plugin(module_name)
                if not result.success:
                    return f"Recovery failed: {result.error}"
            elif module_name in get_plugins():
                unload_plugin(module_name)
            get_plugin_manager().refresh_butler()
            return "Recovery succeeded."
        except Exception as exc:
            return f"Recovery failed: {type(exc).__name__}: {exc}"

    async def _recover_failed_revert(
        self,
        target: Path,
        module_name: str,
        old_content: Optional[str],
        was_loaded: bool,
    ) -> str:
        """恢复回滚操作前的正式文件。"""
        try:
            if old_content is None:
                target.unlink(missing_ok=True)
                if module_name in get_plugins():
                    unload_plugin(module_name)
            else:
                await get_self_mod_manager().apply(
                    str(target),
                    old_content,
                    "Recover failed plugin revert",
                    source="recovery",
                )
                result = try_load_plugin(module_name)
                if was_loaded and not result.success:
                    return f"Recovery failed: {result.error}"
            get_plugin_manager().refresh_butler()
            return "Recovery succeeded."
        except Exception as exc:
            logger.critical(f"[PluginDeployer] revert recovery failed for {module_name}: {exc}")
            return f"Recovery failed: {type(exc).__name__}: {exc}"

    def _quarantine_candidate(
        self,
        target: Path,
        module_name: str,
        content: str,
        base_hash: Optional[str],
        error: str,
    ) -> str:
        self._quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_id = f"q-{datetime.now().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record = QuarantineRecord(
            quarantine_id=quarantine_id,
            target_path=str(target),
            module_name=module_name,
            created_at=datetime.now().isoformat(),
            candidate_sha256=_sha256_text(content),
            expected_base_sha256=base_hash,
            error=error[-1000:],
        )
        self._atomic_write(self._quarantine_dir / f"{quarantine_id}.py", content)
        self._atomic_write(
            self._quarantine_dir / f"{quarantine_id}.json",
            json.dumps(asdict(record), ensure_ascii=False, indent=2),
        )
        self._remove_staging(target.name)
        return quarantine_id

    def _read_records(self) -> list[QuarantineRecord]:
        if not self._quarantine_dir.exists():
            return []
        records = []
        for path in sorted(self._quarantine_dir.glob("q-*.json")):
            try:
                records.append(QuarantineRecord(**json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, TypeError, json.JSONDecodeError) as exc:
                logger.error(f"[PluginDeployer] invalid quarantine metadata {path}: {exc}")
        return records

    def _get_record(self, quarantine_id: str) -> tuple[QuarantineRecord, Path]:
        if not re.fullmatch(r"q-[0-9]{14}-[a-f0-9]{8}", quarantine_id):
            raise SelfModError("Invalid quarantine id.")
        metadata_path = self._quarantine_dir / f"{quarantine_id}.json"
        candidate_path = self._quarantine_dir / f"{quarantine_id}.py"
        if not metadata_path.is_file() or not candidate_path.is_file():
            raise SelfModError(f"Quarantine item not found: {quarantine_id}")
        try:
            record = QuarantineRecord(**json.loads(metadata_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, json.JSONDecodeError) as exc:
            raise SelfModError(f"Invalid quarantine metadata: {exc}") from exc
        if _sha256_file(candidate_path) != record.candidate_sha256:
            raise SelfModError("The quarantine candidate hash does not match its metadata.")
        return record, metadata_path

    @staticmethod
    def _owned_names(module_name: str) -> tuple[list[str], list[str]]:
        commands = [str(item.alc.command) for item in _commands if item.plugin_package == module_name]
        tools = [name for name, item in _caller_data.items() if item.plugin_package == module_name]
        return commands, tools

    def _remove_staging(self, name: str) -> None:
        (self._staging_dir / name).unlink(missing_ok=True)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)

    def _probe_environment(self) -> dict[str, str]:
        """让子进程同时找到 MAS 源码和当前项目插件。"""
        environment = os.environ.copy()
        source_root = str(Path(__file__).resolve().parents[3])
        old_path = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, old_path)))
        environment["MUIKA_PLUGIN_ROOT"] = str(self._plugins_dir)
        return environment

    def _ensure_plugins_import_path(self) -> None:
        """把配置的插件目录加入顶层包搜索路径。"""
        package = importlib.import_module(self._plugins_dir.name)
        package_paths = getattr(package, "__path__", None)
        plugin_root = str(self._plugins_dir)
        if package_paths is not None and plugin_root not in package_paths:
            package_paths.append(plugin_root)

    @staticmethod
    async def _read_probe_output(process: asyncio.subprocess.Process) -> bytes:
        """读取探针输出，并只保留末尾 8 KiB。"""
        if process.stdout is None:
            await process.wait()
            return b""
        output = b""
        while chunk := await process.stdout.read(4096):
            output = (output + chunk)[-_OUTPUT_LIMIT:]
        await process.wait()
        return output


_deployer: Optional[PluginDeployer] = None


def get_plugin_deployer() -> PluginDeployer:
    """获取插件部署器单例。"""
    global _deployer
    if _deployer is None:
        _deployer = PluginDeployer()
    return _deployer
