""".usage —— Token 用量统计命令。"""

from arclet.alconna import Alconna, Args

from muika.config import get_model_config_manager
from muika.database.crud import UsageORM
from muika.database.db import get_session
from muika.llm import Usage
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(
    name="usage",
    description="查看 Token 用量统计",
    usage=".usage [today|week|total]",
)

alc = Alconna("usage", Args["period?", str])
usage_cmd = on_alconna(alc)


@usage_cmd.handle()
async def _show_usage(period: str = "today") -> str:
    """按模型汇总所选时段的 Token 用量。"""
    periods = {"today": 1, "week": 7, "total": None}
    if period not in periods:
        return "[System] 用法: .usage [today|week|total]"
    async with get_session() as session:
        records = await UsageORM.get_usage_records(session, days=periods[period])

    if not records:
        return f"{period}暂无用量数据"

    manager = get_model_config_manager()
    by_model: dict[str, Usage] = {}
    costs: dict[str, float] = {}
    for r in records:
        usage = by_model.setdefault(r.model, Usage())
        usage.input_tokens += r.input_tokens or 0
        usage.output_tokens += r.output_tokens or 0
        usage.cached_tokens += r.cached_tokens or 0
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
            costs[r.model] = costs.get(r.model, 0.0) + cost

    lines = [f"Token 用量（{period}）:"]
    totals = Usage()
    for model, usage in sorted(by_model.items()):
        row = f"  {model}: 输入 {usage.input_tokens:,} | 输出 {usage.output_tokens:,}"
        if usage.cached_tokens and usage.input_tokens:
            row += f" | 缓存命中 {usage.cached_tokens:,}({usage.cached_rate:.2%})"
        if model in costs:
            row += f" → ${costs[model]:.4f}"
        lines.append(row)

        totals.input_tokens += usage.input_tokens
        totals.output_tokens += usage.output_tokens
        totals.cached_tokens += usage.cached_tokens

    total_line = f"合计: 输入 {totals.input_tokens:,} | 输出 {totals.output_tokens:,}"
    if totals.cached_tokens:
        total_line += f" | 缓存命中 {totals.cached_tokens:,}"
    lines.append(total_line)

    total_cost = sum(costs.values())
    if total_cost:
        lines.append(f"预计总费用: ${total_cost:.2f}")

    return "\n".join(lines)
