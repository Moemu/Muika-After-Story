""".nodes：查看活动设备，或请求切换设备。"""

from arclet.alconna import Alconna, Args, Subcommand

from muika.core.state import MuikaState
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(name="nodes", description="活动设备管理", usage=".nodes <list|handoff 设备名|help>")
nodes_cmd = on_alconna(
    Alconna("nodes", Subcommand("list"), Subcommand("help"), Subcommand("handoff", Args["target", str]))
)


@nodes_cmd.assign("help")
async def help_nodes() -> str:
    return ".nodes list：查看活动设备\n.nodes handoff 设备名：请求切换到该设备"


@nodes_cmd.assign("list")
async def list_nodes(state: MuikaState) -> str:
    nodes = state.nodes
    if nodes is None:
        return "当前只在本机运行。"
    if not nodes.connected:
        return f"当前设备：{nodes.name}。暂时联系不到常驻入口，其他设备的状态未知。"
    return f"正在活动：{nodes.name}\n已连接设备：{', '.join(nodes.nodes)}"


@nodes_cmd.assign("handoff")
async def handoff_node(target: str, state: MuikaState) -> str:
    if state.nodes is None:
        return "当前只在本机运行，无法切换设备。"
    try:
        await state.nodes.handoff(target)
    except (ValueError, OSError) as exc:
        return f"暂时无法切换设备：{exc}"
    return f"已请求切换到 {target}。可以用 .nodes list 查看结果。"
