from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from nonebot import logger
from pydantic import Field

from muika.config import mas_config

from ..schema import ActionOutput
from ._base import BaseTool

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class _FSError(Exception):
    """Raised when a file system operation is rejected by policy or path validation."""


def _resolve_and_check(raw_path: str, require_write: bool = False) -> Path:
    """
    Resolve a raw path string and validate it against the whitelist.

    Raises _FSError with a human-readable message when the path should be rejected.
    Returns the resolved Path on success.
    """
    allowed = [Path(p).resolve() for p in mas_config.fs_allowed_paths]

    if not allowed:
        raise _FSError("File system tools are disabled: FS_ALLOWED_PATHS is empty.")

    try:
        resolved = Path(raw_path).resolve()
    except Exception as e:
        raise _FSError(f"Invalid path {raw_path!r}: {e}") from e

    # Path traversal guard: resolved path must be inside at least one allowed root.
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise _FSError(
            f"Access denied: {resolved} is not inside any allowed directory. " f"Allowed: {[str(p) for p in allowed]}"
        )

    if require_write and not mas_config.enable_file_write:
        raise _FSError("File write/delete is disabled. Set ENABLE_FILE_WRITE=true to enable.")

    return resolved


# ---------------------------------------------------------------------------
# Tier 1 — Read-only (active when fs_allowed_paths is non-empty)
# ---------------------------------------------------------------------------


class ListDirectoryTool(BaseTool):
    """List the contents of a directory within the allowed paths."""

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(mas_config.fs_allowed_paths)

    name: Literal["list_directory"] = "list_directory"
    path: str = Field(..., description="Absolute or relative path of the directory to list.")
    show_hidden: bool = Field(False, description="Whether to include hidden files (starting with '.').")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            resolved = _resolve_and_check(self.path)
        except _FSError as e:
            return ActionOutput(content=f"[ListDirectoryTool] {e}")

        if not resolved.exists():
            return ActionOutput(content=f"[ListDirectoryTool] Path does not exist: {resolved}")
        if not resolved.is_dir():
            return ActionOutput(content=f"[ListDirectoryTool] Not a directory: {resolved}")

        try:
            entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
            lines = [f"Directory: {resolved}", ""]
            for entry in entries:
                if not self.show_hidden and entry.name.startswith("."):
                    continue
                kind = "DIR " if entry.is_dir() else "FILE"
                size = ""
                if entry.is_file():
                    try:
                        size = f"  {entry.stat().st_size:,} bytes"
                    except OSError:
                        size = "  (size unknown)"
                lines.append(f"  [{kind}] {entry.name}{size}")

            if len(lines) == 2:
                lines.append("  (empty)")

            logger.debug(f"[ListDirectoryTool] Listed {resolved}")
            return ActionOutput(content="\n".join(lines))
        except PermissionError:
            return ActionOutput(content=f"[ListDirectoryTool] Permission denied: {resolved}")
        except Exception as e:
            logger.error(f"[ListDirectoryTool] Failed: {e}")
            return ActionOutput(content=f"[ListDirectoryTool] Error: {e}")


class ReadFileTool(BaseTool):
    """Read the text content of a file within the allowed paths."""

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(mas_config.fs_allowed_paths)

    name: Literal["read_file"] = "read_file"
    path: str = Field(..., description="Absolute or relative path of the file to read.")
    encoding: str = Field("utf-8", description="File encoding, default utf-8.")
    max_chars: int = Field(
        4000,
        description="Maximum characters to return. Content beyond this limit is truncated.",
    )

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            resolved = _resolve_and_check(self.path)
        except _FSError as e:
            return ActionOutput(content=f"[ReadFileTool] {e}")

        if not resolved.exists():
            return ActionOutput(content=f"[ReadFileTool] File not found: {resolved}")
        if not resolved.is_file():
            return ActionOutput(content=f"[ReadFileTool] Not a file: {resolved}")

        try:
            text = resolved.read_text(encoding=self.encoding, errors="replace")
        except PermissionError:
            return ActionOutput(content=f"[ReadFileTool] Permission denied: {resolved}")
        except Exception as e:
            logger.error(f"[ReadFileTool] Failed: {e}")
            return ActionOutput(content=f"[ReadFileTool] Error reading file: {e}")

        total = len(text)
        truncated = text[: self.max_chars]
        suffix = f"\n...(truncated, {total - self.max_chars:,} chars omitted)" if total > self.max_chars else ""
        logger.debug(f"[ReadFileTool] Read {total:,} chars from {resolved}")
        return ActionOutput(content=f"File: {resolved}\n\n{truncated}{suffix}")


# ---------------------------------------------------------------------------
# Tier 2 — Write operations (requires ENABLE_FILE_WRITE=true)
# ---------------------------------------------------------------------------


class WriteFileTool(BaseTool):
    """Write or append text content to a file within the allowed paths.
    Requires ENABLE_FILE_WRITE=true.
    """

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(mas_config.fs_allowed_paths) and mas_config.enable_file_write

    name: Literal["write_file"] = "write_file"
    path: str = Field(..., description="Absolute or relative path of the file to write.")
    content: str = Field(..., description="Text content to write.")
    write_mode: Literal["overwrite", "append"] = Field(
        "overwrite",
        description="'overwrite' replaces the file; 'append' adds to the end.",
    )
    encoding: str = Field("utf-8", description="File encoding, default utf-8.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            resolved = _resolve_and_check(self.path, require_write=True)
        except _FSError as e:
            return ActionOutput(content=f"[WriteFileTool] {e}")

        open_mode = "a" if self.write_mode == "append" else "w"
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            with resolved.open(open_mode, encoding=self.encoding) as f:
                f.write(self.content)
            logger.info(f"[WriteFileTool] Wrote {len(self.content):,} chars to {resolved} (mode={self.write_mode})")
            return ActionOutput(
                content=f"File written successfully ({self.write_mode}): {resolved}  ({len(self.content):,} chars)"
            )
        except PermissionError:
            return ActionOutput(content=f"[WriteFileTool] Permission denied: {resolved}")
        except Exception as e:
            logger.error(f"[WriteFileTool] Failed: {e}")
            return ActionOutput(content=f"[WriteFileTool] Error: {e}")


class DeleteFileTool(BaseTool):
    """Permanently delete a single file within the allowed paths.
    Requires ENABLE_FILE_WRITE=true. This action is irreversible.
    """

    @classmethod
    def is_enabled(cls) -> bool:
        return bool(mas_config.fs_allowed_paths) and mas_config.enable_file_write

    name: Literal["delete_file"] = "delete_file"
    path: str = Field(..., description="Absolute or relative path of the file to delete.")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        try:
            resolved = _resolve_and_check(self.path, require_write=True)
        except _FSError as e:
            return ActionOutput(content=f"[DeleteFileTool] {e}")

        if not resolved.exists():
            return ActionOutput(content=f"[DeleteFileTool] File not found: {resolved}")
        if resolved.is_dir():
            return ActionOutput(
                content=f"[DeleteFileTool] {resolved} is a directory. Only single files can be deleted."
            )

        try:
            resolved.unlink()
            logger.warning(f"[DeleteFileTool] Deleted: {resolved}")
            return ActionOutput(content=f"File deleted: {resolved}")
        except PermissionError:
            return ActionOutput(content=f"[DeleteFileTool] Permission denied: {resolved}")
        except Exception as e:
            logger.error(f"[DeleteFileTool] Failed: {e}")
            return ActionOutput(content=f"[DeleteFileTool] Error: {e}")
