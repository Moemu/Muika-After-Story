"""查看与回滚 Core 工作区版本。"""

from __future__ import annotations

from arclet.alconna import Alconna, Args, Arparma, CommandMeta, Option, Subcommand

from muika.core.loop import Muika
from muika.core.self_mod.workspace import SelfWorkspaceError, get_self_workspace_manager
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="patch",
    description="查看与回滚 Core 工作区版本",
    usage=".patch <list|show|rollback|restart>",
)

alc = Alconna(
    "patch",
    Subcommand("list", dest="list"),
    Subcommand("show", Args["ws_id", str], Args["page", int, 1], dest="show"),
    Subcommand(
        "rollback",
        Option("--confirm-memory-loss", dest="confirm_memory_loss"),
        dest="rollback",
    ),
    Subcommand("restart", dest="restart"),
    meta=CommandMeta("查看与回滚 Core 工作区版本"),
)

patch_cmd = on_alconna(alc)


@patch_cmd.assign("list")
async def _list() -> str:
    """列出工作区版本、待重启指针与最近提升记录。"""
    from muika.ipc import supervisor

    lines: list[str] = []
    if supervisor.read_boot_target() is not None:
        lines.append("[System] 有一个已通过终审的新版本正在等待重启。")
    ready = supervisor.read_rollback_ready()
    if ready is not None:
        lines.append(f"[System] 最近提升的版本：{ready.get('workspace_id')}（{ready.get('status')}）。")
    manifests = get_self_workspace_manager().list_workspaces()
    if manifests:
        lines.append("工作区：")
        lines.extend(f"- {m['ws_id']} [{m['status']}] {m['reason']}" for m in manifests)
    return "\n".join(lines) if lines else "[System] 还没有工作区版本记录。"


@patch_cmd.assign("show")
async def _show(ws_id: str, page: int = 1) -> str:
    """分页显示一个工作区与当前运行树的差异。"""
    try:
        return get_self_workspace_manager().show_diff(ws_id, page)
    except SelfWorkspaceError as exc:
        return f"[System] 无法显示差异：{exc}"


@patch_cmd.assign("rollback")
async def _rollback(muika: Muika, arparma: Arparma) -> str:
    """回滚最近一次提升的版本；破坏性动作由父进程执行。"""
    confirm = arparma.query("rollback.confirm_memory_loss") is not None
    from muika.ipc import supervisor

    try:
        ws_id = await get_self_workspace_manager().request_rollback(confirm)
    except SelfWorkspaceError as exc:
        return f"[System] 回滚被拒绝：{exc}"
    try:
        await muika.restart.request(ws_id, supervisor.ROLLBACK_TRIGGER, confirm_memory_loss=confirm)
    except (OSError, ValueError) as exc:
        return f"[System] 无法回滚：{exc}"
    return "[System] 已请求回滚，保存进度并重启后旧版本会回来。"


@patch_cmd.assign("restart")
async def _restart(muika: Muika) -> str:
    """重启并让等待中的新版本生效。"""
    if muika.restart.handler is None:
        return "[System] 当前宿主未提供重启管理，请通过启动入口手动重启 Core。"
    await muika.executor.send_message("好哦，我会保存进度再重新醒来，待会见。")
    await muika.restart.request(None, ".patch restart")
    return "[System] 正在准备重启 Core。"
