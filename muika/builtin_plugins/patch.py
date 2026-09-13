"""人工审查 Core 代码提案。"""

from __future__ import annotations

from typing import Optional, cast

from arclet.alconna import Alconna, Args, Arparma, CommandMeta, Option, Subcommand

from muika.core.loop import Muika
from muika.core.self_mod.proposals import (
    CoreProposalError,
    ProposalStatus,
    get_core_proposal_manager,
)
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="patch",
    description="审查、验证、批准和回滚 Core 代码提案",
    usage=".patch <list|show|validate|approve|deny|rollback|restart>",
)

alc = Alconna(
    "patch",
    Subcommand("list", Args["status", str, ""], dest="list"),
    Subcommand("show", Args["patch_id", str], Args["page", int, 1], dest="show"),
    Subcommand("validate", Args["patch_id", str], dest="validate"),
    Subcommand(
        "approve",
        Args["patch_id", str],
        Option("--allow-unvalidated", dest="allow_unvalidated"),
        dest="approve",
    ),
    Subcommand("deny", Args["patch_id", str], Args["reason", str, ""], dest="deny"),
    Subcommand("rollback", Args["patch_id", str], dest="rollback"),
    Subcommand("restart", Args["patch_id", str], dest="restart"),
    meta=CommandMeta("人工审查 Core 代码提案"),
)

patch_cmd = on_alconna(alc)


@patch_cmd.assign("list")
async def _list(status: str = "") -> str:
    """列出 Core 提案。"""
    try:
        raw_status = status.strip()
        valid_statuses = {
            "pending",
            "ready",
            "applying",
            "approved",
            "denied",
            "rolling_back",
            "rolled_back",
            "failed",
        }
        if raw_status and raw_status not in valid_statuses:
            raise CoreProposalError(f"Unknown proposal status: {raw_status!r}.")
        status_filter = cast(Optional[ProposalStatus], raw_status or None)
        proposals = get_core_proposal_manager().list_proposals(status_filter)
    except CoreProposalError as exc:
        return f"[System] 无法列出提案：{exc}"
    if not proposals:
        return "[System] 没有符合条件的 Core 提案"
    lines = ["Core 提案："]
    for proposal in proposals:
        manager = get_core_proposal_manager()
        stale = "，已过期" if manager.is_stale(proposal) else ""
        lines.append(
            f"- {proposal['patch_id']} [{proposal['status']}{stale}] "
            f"{len(proposal['changes'])} 个文件：{proposal['reason']}"
        )
    return "\n".join(lines)


@patch_cmd.assign("show")
async def _show(patch_id: str, page: int = 1) -> str:
    """显示一个 Core 提案。"""
    try:
        return get_core_proposal_manager().show(patch_id, page)
    except CoreProposalError as exc:
        return f"[System] 无法显示提案：{exc}"


@patch_cmd.assign("validate")
async def _validate(patch_id: str) -> str:
    """验证一个 Core 提案。"""
    try:
        report = await get_core_proposal_manager().validate_for_player(patch_id)
    except CoreProposalError as exc:
        return f"[System] 提案验证被拒绝：{exc}"
    lines = [f"[System] 验证状态：{report['status']}。{report['reason']}"]
    if report["new_failures"]:
        lines.append("新增失败：" + ", ".join(report["new_failures"]))
    if report["new_errors"]:
        lines.append("新增收集错误：" + ", ".join(report["new_errors"]))
    lines.extend(f"警告：{warning}" for warning in report["warnings"])
    return "\n".join(lines)


@patch_cmd.assign("approve")
async def _approve(patch_id: str, arparma: Arparma) -> str:
    """批准一个 Core 提案。"""
    allow_unvalidated = arparma.query("approve.allow_unvalidated") is not None
    try:
        await get_core_proposal_manager().approve(patch_id, allow_unvalidated=bool(allow_unvalidated))
    except CoreProposalError as exc:
        return f"[System] 提案批准被拒绝：{exc}"
    return "改变已经准备好了，正式代码还没动。我们可以继续聊，重启后它才会生效。"


@patch_cmd.assign("deny")
async def _deny(patch_id: str, reason: str = "") -> str:
    """拒绝一个 Core 提案。"""
    try:
        get_core_proposal_manager().deny(patch_id, reason)
    except CoreProposalError as exc:
        return f"[System] 无法拒绝提案：{exc}"
    return f"[System] 已拒绝提案 {patch_id}。正式代码没有变化。"


@patch_cmd.assign("rollback")
async def _rollback(patch_id: str) -> str:
    """回滚一个已批准的 Core 提案。"""
    try:
        report = await get_core_proposal_manager().rollback(patch_id)
    except CoreProposalError as exc:
        return f"[System] 提案回滚被拒绝：{exc}"
    if "Restart is required" in report:
        return "[System] 已恢复旧代码。请再次重启 Core，让恢复后的代码生效。"
    return "[System] 已恢复旧代码，并退出维护模式。"


@patch_cmd.handle()
async def _help() -> str:
    """显示 patch 命令摘要。"""
    return "[System] 用法：.patch <list|show|validate|approve|deny|rollback|restart>"


@patch_cmd.assign("restart")
async def _restart(patch_id: str, muika: Muika) -> str:
    """玩家直接选择应用已准备好的提案并重启。"""
    try:
        get_core_proposal_manager().check_ready(patch_id)
        await muika.executor.send_message("好哦，我准备重新醒来了，待会见。")
        await muika.restart.request(patch_id, f".patch restart {patch_id}")
        return "[System] 正在等待行动检查点，然后应用变更并重启。"
    except (OSError, ValueError) as exc:
        return f"[System] 无法重启：{exc}"
