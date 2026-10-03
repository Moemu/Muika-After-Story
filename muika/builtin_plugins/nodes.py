"""设备状态和玩家主动交接命令。"""

from arclet.alconna import Alconna, Args

from muika.core.loop import Muika
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(name="nodes", description="查看设备与选择活动位置", usage=".nodes [handoff|select] [设备名]")
nodes_command = on_alconna(Alconna("nodes", Args["action?", str]["id?", str]))


@nodes_command.handle()
async def nodes(muika: Muika, action: str = "", id: str = "") -> str:
    """直接查询或请求设备操作，命令不经过人格模型。"""
    devices = muika.executor.devices
    if devices is None:
        return "[System] 当前使用单机模式。"
    if not action:
        return await devices.list_devices()
    if not id or action not in {"handoff", "select"}:
        return "[System] 用法: .nodes [handoff|select] [设备名]"
    try:
        if action == "handoff":
            return await devices.request_handoff(id, "The player requested this device.")
        return await devices.select_device(id)
    except ValueError as exc:
        return f"[System] 无法使用该设备: {exc}"
