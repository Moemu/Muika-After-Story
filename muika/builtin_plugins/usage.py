""".usage —— Token 用量统计命令。"""

from arclet.alconna import Alconna

from muika.config import get_model_config_manager
from muika.database.crud import UsageORM
from muika.database.db import get_session
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="usage",
    description="查看 Token 用量统计",
    usage=".usage",
)

alc = Alconna("usage")
usage_cmd = on_alconna(alc)


@usage_cmd.handle()
async def _show_usage() -> str:
    async with get_session() as session:
        records = await UsageORM.get_usage_records(session, days=7)

    manager = get_model_config_manager()
    totals = {"input": 0, "output": 0, "cached": 0, "cost": 0.0}

    if not records:
        return "暂无用量数据"

    lines = ["近 7 天 Token 用量:"]
    current_date = ""
    for r in records:
        date = r.date
        if date != current_date:
            current_date = date
            lines.append(f"{date}:")

        model_label = r.model or ""
        input_t = r.input_tokens or 0
        output_t = r.output_tokens or 0
        cached_t = r.cached_tokens or 0

        row = f"  {model_label}: 输入 {input_t:,} | 输出 {output_t:,}"
        if cached_t and input_t:
            row += f" | 缓存命中 {cached_t:,}({cached_t / input_t:.2%})"

        config = manager.configs.get(r.model) or manager.configs.get(r.plugin or "")
        if config and config.input_price is not None:
            cost = round(
                (
                    (r.input_tokens or 0) * config.input_price
                    + (r.output_tokens or 0) * (config.output_price or 0)
                    + (r.cached_tokens or 0) * (config.cached_price or 0.0)
                )
                / 1_000_000,
                4,
            )
            row += f" → ${cost:.4f}"
            totals["cost"] += cost
        lines.append(row)

        totals["input"] += input_t
        totals["output"] += output_t
        totals["cached"] += cached_t

    total_line = f"合计: 输入 {totals['input']:,} | 输出 {totals['output']:,}"
    if totals["cached"]:
        total_line += f" | 缓存命中 {totals['cached']:,}"
    lines.append(total_line)

    if totals["cost"]:
        lines.append(f"预计总费用: ${totals['cost']:.2f}")

    return "\n".join(lines)
