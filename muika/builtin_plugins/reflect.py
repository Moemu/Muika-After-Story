"""``.reflect`` —— 手动触发 Muika 自省。"""

from __future__ import annotations

import asyncio

from arclet.alconna import Alconna, CommandMeta

from muika.core.loop import Muika
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="reflect",
    description="手动触发 Muika 自省",
    usage=".reflect",
)

reflect_cmd = on_alconna(
    Alconna("reflect", meta=CommandMeta("触发 Muika 的自我反省")),
)


@reflect_cmd.handle()
async def _reflect(muika: Muika) -> str:
    """触发强制自省，fire-and-forget；handler 立即回复 [System] 预告。"""
    asyncio.create_task(muika.reflection.force_reflect("user_command"))
    return "[System] Muika 正在安静地自省……"
