"""玩家查看和处理代码审查请求。"""

from arclet.alconna import Alconna, Args, Subcommand

from muika.core.code_review import get_code_reviewer
from muika.core.loop import Muika
from muika.plugin.command import on_alconna
from muika.plugin.models import PluginMetadata

metadata = PluginMetadata(name="review", description="查看和处理代码审批", usage=".review <list|show|approve|deny>")
review_cmd = on_alconna(
    Alconna(
        "review",
        Subcommand("list"),
        Subcommand("show", Args["review_id", str]),
        Subcommand("approve", Args["review_id", str]),
        Subcommand("deny", Args["review_id", str]),
    )
)


@review_cmd.assign("list")
async def _list() -> str:
    """列出请求的种类和处理状态。"""
    return "\n".join(f"{r.id} [{r.status}] {r.kind}" for r in get_code_reviewer().records()) or "暂无代码审批请求。"


@review_cmd.assign("show")
async def _show(review_id: str) -> str:
    """展示具体操作、影响和完整审查证据。"""
    try:
        return get_code_reviewer().load(review_id).model_dump_json(indent=2)
    except (OSError, ValueError) as exc:
        return f"无法读取审批请求：{exc}"


@review_cmd.assign("approve")
async def _approve(review_id: str, muika: Muika) -> str:
    """批准具体请求并唤醒原任务。"""
    try:
        record = get_code_reviewer().decide(review_id, True)
        if record.owner:
            instruction = (
                f"Continue prepare_core_change(patch_id={record.payload['patch_id']!r}); do not create a new proposal."
                if record.kind in {"core", "core_validation"}
                else ""
            )
            await muika.agent_tasks.resume_review(record.owner, record.id, instruction)
            return "已批准这次操作。原任务将继续；内容发生变化时需要重新审查。"
        return "已批准这次操作。请再次执行原命令，内容发生变化时需要重新审查。"
    except (OSError, ValueError) as exc:
        return f"无法批准：{exc}"


@review_cmd.assign("deny")
async def _deny(review_id: str) -> str:
    """拒绝尚未执行的具体操作。"""
    try:
        get_code_reviewer().decide(review_id, False)
        return "已拒绝这次操作。"
    except (OSError, ValueError) as exc:
        return f"无法拒绝：{exc}"


@review_cmd.handle()
async def _help() -> str:
    """显示专家接管命令。"""
    return "用法：.review <list|show|approve|deny>；自动审查默认开启。"
