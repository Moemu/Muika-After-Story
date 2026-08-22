""".heart —— Heart 内心独白强度控制命令。"""

from arclet.alconna import Alconna, Args

from muika.config import get_model_config_manager
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="heart",
    description="Heart 内心独白强度控制",
    usage=".heart [low|medium|high|off]",
)

alc = Alconna("heart", Args["level?", str])
heart_cmd = on_alconna(alc)


@heart_cmd.handle()
async def _heart(level: str | None = None) -> str:
    manager = get_model_config_manager()
    if level is None:
        return f"[System] Heart 强度: {manager.heart_intensity}"
    if level not in ("low", "medium", "high", "off"):
        return f"[System] 用法: .heart <low|medium|high|off>（当前：{manager.heart_intensity}）"
    manager.set_heart_intensity(level)  # type: ignore[arg-type]
    return f"[System] Heart 强度已切换为 {level}"
