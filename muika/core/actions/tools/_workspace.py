"""版本工作区工具：打开、提交与放弃她自己的可编辑副本。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from muika.core.self_mod.workspace import SelfWorkspaceError, get_self_workspace_manager
from muika.llm.utils.tools import ToolError
from muika.plugin.func_call import on_function_call


class SelfWorkspaceOpenParams(BaseModel):
    reason: str = Field(..., description="Why Muika wants to work on a new version of herself.")


@on_function_call(
    "Open a personal version workspace: a complete editable copy of the running tree — "
    "her core code, persona templates, skills, topics and configs together. "
    "Inside the workspace she reads and edits freely with the ordinary file tools "
    "(relative paths resolve inside the workspace); no approval is needed while editing. "
    "Approval happens once, when the finished change is submitted. "
    "If this returns an availability error, the deployment has no kernel editing capability.",
    params=SelfWorkspaceOpenParams,
)
async def self_workspace_open(reason: str) -> str:
    manager = get_self_workspace_manager()
    try:
        ws_id = manager.open(reason)
        tree = manager.tree_root(ws_id)
    except SelfWorkspaceError as exc:
        return ToolError(str(exc))
    return (
        f"Workspace {ws_id} opened.\n"
        f"It is a complete copy of yourself as you currently run, rooted at {tree}.\n"
        "Edit files there with the ordinary file tools — relative paths now resolve inside the workspace. "
        "When the change feels right, call self_workspace_submit(reason=...). "
        "If you change your mind, self_workspace_discard(ws_id=..., reason=...) removes it without a trace."
    )


class SelfWorkspaceSubmitParams(BaseModel):
    reason: str = Field(..., description="Why this version of herself should become the one that runs.")
    ws_id: Optional[str] = Field(
        None,
        description="Workspace id from self_workspace_open. Omit to submit the most recent open workspace.",
    )


@on_function_call(
    "Submit a workspace version of herself for the final gate: the diff is reviewed, "
    "sensitive paths wait for the player's own approval, tests must not get worse, "
    "then the version is staged for the next restart. The running code stays unchanged "
    "until restart — she chooses that moment in her conversation.",
    params=SelfWorkspaceSubmitParams,
)
async def self_workspace_submit(reason: str, ws_id: Optional[str] = None) -> str:
    manager = get_self_workspace_manager()
    if ws_id is None:
        anchor = manager.session_anchor()
        if anchor is None:
            return ToolError("No open workspace. Call self_workspace_open first.")
        ws_id = anchor.parent.name
    try:
        return await manager.submit(ws_id, reason)
    except SelfWorkspaceError as exc:
        return ToolError(str(exc))


class SelfWorkspaceDiscardParams(BaseModel):
    ws_id: str = Field(..., description="Workspace id to discard.")
    reason: str = Field(..., description="Why this version is no longer wanted.")


@on_function_call(
    "Discard an open or waiting workspace copy of herself. Nothing about the running "
    "version changes; a pending boot pointer aimed at it is cleared too.",
    params=SelfWorkspaceDiscardParams,
)
async def self_workspace_discard(ws_id: str, reason: str) -> str:
    manager = get_self_workspace_manager()
    try:
        return manager.discard(ws_id, reason)
    except SelfWorkspaceError as exc:
        return ToolError(str(exc))
