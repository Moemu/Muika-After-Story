"""Muika 内置命令插件。"""

from muika.plugin import PluginMetadata

from . import (  # noqa: F401
    debug,
    heart,
    help,
    model,
    nodes,
    patch,
    plugins,
    reflect,
    restart,
    review,
    session,
    usage,
)

metadata = PluginMetadata(name="muika_builtin_commands", description="MAS 内置指令插件", usage="输入 .help 获取帮助")
