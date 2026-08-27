from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.core.self_mod.policy import is_protected_path
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger


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
        raise _FSError("File system tools are disabled by configuration.")

    try:
        resolved = Path(raw_path).resolve()
    except Exception as e:
        raise _FSError(f"Invalid path {raw_path!r}: {e}") from e

    # Path traversal guard: resolved path must be inside at least one allowed root.
    if not any(resolved == root or root in resolved.parents for root in allowed):
        raise _FSError(
            f"Access denied: {resolved} is not inside any allowed directory. " f"Allowed: {[str(p) for p in allowed]}"
        )

    if require_write and is_protected_path(resolved):
        raise _FSError(f"Access denied: {resolved} is protected core code and can never be modified.")

    if require_write and not mas_config.enable_file_write:
        raise _FSError("File write/delete is disabled by configuration.")

    return resolved


# ---------------------------------------------------------------------------
# Read-only operations
# ---------------------------------------------------------------------------


class ListDirectoryParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path of the directory to list.")
    show_hidden: bool = Field(False, description="Whether to include hidden files (starting with '.').")


@on_function_call(
    "List the contents of a directory within the allowed paths.",
    params=ListDirectoryParams,
)
async def list_directory(path: str, show_hidden: bool = False):
    if not mas_config.fs_allowed_paths:
        return "File system tools are disabled by configuration."

    try:
        resolved = _resolve_and_check(path)
    except _FSError as e:
        return str(e)

    if not resolved.exists():
        return f"Path does not exist: {resolved}"
    if not resolved.is_dir():
        return f"Not a directory: {resolved}"

    try:
        entries = sorted(resolved.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        lines = [f"Directory: {resolved}", ""]
        for entry in entries:
            if not show_hidden and entry.name.startswith("."):
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

        logger.debug(f"[ListDirectory] Listed {resolved}")
        return "\n".join(lines)
    except PermissionError:
        return f"Permission denied: {resolved}"
    except Exception as e:
        logger.error(f"[ListDirectory] Failed: {e}")
        return f"Error: {e}"


class ReadFileParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path of the file to read.")
    encoding: str = Field("utf-8", description="File encoding, default utf-8.")
    max_chars: int = Field(
        4000,
        description="Maximum characters to return. Content beyond this limit is truncated.",
    )


@on_function_call(
    "Read the text content of a file within the allowed paths.",
    params=ReadFileParams,
)
async def read_file(path: str, encoding: str = "utf-8", max_chars: int = 4000):
    if not mas_config.fs_allowed_paths:
        return "File system tools are disabled by configuration."

    try:
        resolved = _resolve_and_check(path)
    except _FSError as e:
        return str(e)

    if not resolved.exists():
        return f"File not found: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"

    try:
        text = resolved.read_text(encoding=encoding, errors="replace")
    except PermissionError:
        return f"Permission denied: {resolved}"
    except Exception as e:
        logger.error(f"[ReadFile] Failed: {e}")
        return f"Error reading file: {e}"

    total = len(text)
    truncated = text[:max_chars]
    suffix = f"\n...(truncated, {total - max_chars:,} chars omitted)" if total > max_chars else ""
    logger.debug(f"[ReadFile] Read {total:,} chars from {resolved}")
    return f"File: {resolved}\n\n{truncated}{suffix}"


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------


class WriteFileParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path of the file to write.")
    content: str = Field(..., description="Text content to write.")
    write_mode: Literal["overwrite", "append"] = Field(
        "overwrite",
        description="'overwrite' replaces the file; 'append' adds to the end.",
    )
    encoding: str = Field("utf-8", description="File encoding, default utf-8.")


@on_function_call(
    "Write or append text content to a file within the allowed paths. "
    "Requires file-write permission enabled by the user.",
    params=WriteFileParams,
)
async def write_file(path: str, content: str, write_mode: str = "overwrite", encoding: str = "utf-8"):
    if not mas_config.fs_allowed_paths:
        return "File system tools are disabled by configuration."
    if not mas_config.enable_file_write:
        return "File write/delete is disabled by configuration."

    try:
        resolved = _resolve_and_check(path, require_write=True)
    except _FSError as e:
        return str(e)

    open_mode = "a" if write_mode == "append" else "w"
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open(open_mode, encoding=encoding) as f:
            f.write(content)
        logger.info(f"[WriteFile] Wrote {len(content):,} chars to {resolved} (mode={write_mode})")
        return f"File written successfully ({write_mode}): {resolved}  ({len(content):,} chars)"
    except PermissionError:
        return f"Permission denied: {resolved}"
    except Exception as e:
        logger.error(f"[WriteFile] Failed: {e}")
        return f"Error: {e}"


class EditFileParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path of the file to edit.")
    operation: Literal["replace", "insert", "delete_lines"] = Field(
        ...,
        description=(
            "'replace': replace old_string with new_string (must appear exactly once). "
            "'insert': insert new_string before line_number (1-based). "
            "'delete_lines': delete lines from line_start to line_end inclusive (1-based)."
        ),
    )
    old_string: Optional[str] = Field(
        None,
        description="Required for 'replace'. The exact string to find in the file.",
    )
    new_string: Optional[str] = Field(
        None,
        description="Required for 'replace' and 'insert'. The replacement or inserted text.",
    )
    line_number: Optional[int] = Field(
        None,
        description="Required for 'insert'. 1-based line number to insert before. "
        "Use 0 or a number beyond EOF to append.",
    )
    line_start: Optional[int] = Field(
        None,
        description="Required for 'delete_lines'. First line to delete (1-based, inclusive).",
    )
    line_end: Optional[int] = Field(
        None,
        description="Required for 'delete_lines'. Last line to delete (1-based, inclusive).",
    )
    encoding: str = Field("utf-8", description="File encoding, default utf-8.")


@on_function_call(
    "Edit an existing text file within the allowed paths using precise operations. "
    "Requires file-write permission enabled by the user.",
    params=EditFileParams,
)
async def edit_file(
    path: str,
    operation: str,
    old_string: Optional[str] = None,
    new_string: Optional[str] = None,
    line_number: Optional[int] = None,
    line_start: Optional[int] = None,
    line_end: Optional[int] = None,
    encoding: str = "utf-8",
):
    if not mas_config.fs_allowed_paths:
        return "File system tools are disabled by configuration."
    if not mas_config.enable_file_write:
        return "File write/delete is disabled by configuration."

    try:
        resolved = _resolve_and_check(path, require_write=True)
    except _FSError as e:
        return str(e)

    if not resolved.exists():
        return f"File not found: {resolved}"
    if not resolved.is_file():
        return f"Not a file: {resolved}"

    try:
        original = resolved.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        return f"Failed to read file: {e}"

    try:
        result = _apply_edit(original, operation, old_string, new_string, line_number, line_start, line_end)
    except ValueError as e:
        return str(e)

    try:
        resolved.write_text(result, encoding=encoding)
    except PermissionError:
        return f"Permission denied: {resolved}"
    except Exception as e:
        logger.error(f"[EditFile] Failed to write: {e}")
        return f"Error writing file: {e}"

    logger.info(f"[EditFile] Applied '{operation}' to {resolved}")
    return f"File edited successfully ({operation}): {resolved}"


def _apply_edit(
    text: str,
    operation: str,
    old_string: Optional[str],
    new_string: Optional[str],
    line_number: Optional[int],
    line_start: Optional[int],
    line_end: Optional[int],
) -> str:
    if operation == "replace":
        if old_string is None or new_string is None:
            raise ValueError("'replace' requires both old_string and new_string.")
        count = text.count(old_string)
        if count == 0:
            raise ValueError("old_string not found in file.")
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times; it must match exactly once. "
                "Add more surrounding context to make it unique."
            )
        return text.replace(old_string, new_string, 1)

    if operation == "insert":
        if new_string is None or line_number is None:
            raise ValueError("'insert' requires new_string and line_number.")
        lines = text.splitlines(keepends=True)
        idx = max(0, line_number - 1)
        insert_text = new_string if new_string.endswith("\n") else new_string + "\n"
        lines.insert(idx, insert_text)
        return "".join(lines)

    if operation == "delete_lines":
        if line_start is None or line_end is None:
            raise ValueError("'delete_lines' requires line_start and line_end.")
        if line_start < 1 or line_end < line_start:
            raise ValueError("line_start must be ≥ 1 and ≤ line_end.")
        lines = text.splitlines(keepends=True)
        total = len(lines)
        if line_start > total:
            raise ValueError(f"line_start ({line_start}) exceeds total lines ({total}); nothing to delete.")
        start_idx = line_start - 1
        end_idx = min(line_end, total)
        del lines[start_idx:end_idx]
        if line_end > total:
            raise ValueError(
                f"Only {total - start_idx} line(s) were available to delete "
                f"(line_end={line_end} exceeds total={total}). "
                f"Deleted the available range; check the file first."
            )
        return "".join(lines)

    raise ValueError(f"Unknown operation: {operation!r}")


class DeleteFileParams(BaseModel):
    path: str = Field(..., description="Absolute or relative path of the file to delete.")


@on_function_call(
    "Permanently delete a single file within the allowed paths. "
    "Requires file-write permission enabled by the user. This action is irreversible.",
    params=DeleteFileParams,
)
async def delete_file(path: str):
    if not mas_config.fs_allowed_paths:
        return "File system tools are disabled by configuration."
    if not mas_config.enable_file_write:
        return "File write/delete is disabled by configuration."

    try:
        resolved = _resolve_and_check(path, require_write=True)
    except _FSError as e:
        return str(e)

    if not resolved.exists():
        return f"File not found: {resolved}"
    if resolved.is_dir():
        return f"{resolved} is a directory. Only single files can be deleted."

    try:
        resolved.unlink()
        logger.warning(f"[DeleteFile] Deleted: {resolved}")
        return f"File deleted: {resolved}"
    except PermissionError:
        return f"Permission denied: {resolved}"
    except Exception as e:
        logger.error(f"[DeleteFile] Failed: {e}")
        return f"Error: {e}"
