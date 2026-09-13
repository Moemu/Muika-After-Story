"""提供不依赖 Core 提案的玩家重启命令。"""

from arclet.alconna import Alconna

from muika.core.loop import Muika
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(name="restart", description="保存行动进度并重启 Core", usage=".restart")
restart_cmd = on_alconna(Alconna("restart"))


@restart_cmd.handle()
async def _restart(muika: Muika) -> str:
    """响应玩家重启当前文件的命令，不应用待处理提案。"""
    try:
        if muika.restart.handler is None:
            return "[System] 当前宿主未提供重启管理，请通过启动入口手动重启 Core。"
        await muika.executor.send_message("好哦，我会保存进度再重新醒来，待会见。")
        await muika.restart.request(None, ".restart")
        return "[System] 正在准备重启 Core，待处理提案保持不变。"
    except (OSError, ValueError) as exc:
        return f"[System] 无法重启：{exc}"
