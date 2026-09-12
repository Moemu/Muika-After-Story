"""提交待整理笔记，检索记忆和读取原始来源。"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from muika.core.memory import MemoryCategory, MemoryManager, MemoryQuery
from muika.plugin.func_call import on_function_call


class MemoryParams(BaseModel):
    type: Literal["remember", "forget", "read", "source"]
    category: MemoryCategory = MemoryCategory.USER
    key: str | None = Field(None, description="Subject-qualified fact key for forgetting, or a note label.")
    value: str | None = Field(None, description="A note to remember, or a keyword query for read.")
    source: str | None = Field(
        None, description="Inspect an experience:N, diary:N, fact:N, context:hash or task_output:task:call reference."
    )
    offset: int = Field(0, ge=0)
    terms: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Related short keywords or names for recall. Expand synonyms when useful.",
    )
    start: date | None = Field(None, description="Inclusive local date YYYY-MM-DD; resolve relative dates first.")
    end: date | None = None


@on_function_call(
    "Record a note for your next dream, recall memories by keyword, inspect original sources, or forget a fact.",
    params=MemoryParams,
)
async def memory(
    type: str,
    memory: MemoryManager,
    category: str = "user",
    key: str | None = None,
    value: str | None = None,
    source: str | None = None,
    offset: int = 0,
    terms: list[str] | None = None,
    start: date | None = None,
    end: date | None = None,
) -> str:
    """读写记忆素材；写笔记不会立刻形成事实或增加权重。"""
    if type == "remember":
        if not value:
            return "A note value is required."
        ref = await memory.add_material("note", f"{category}/{key or 'note'}: {value}")
        return f"Note saved as experience:{ref}; it will be reviewed in your diary."
    if type == "forget":
        if not key:
            return "A fact key is required."
        await memory.forget_memory(MemoryCategory(category), key)
        return "The fact has been withdrawn from the fact ledger and resident summary."
    if type == "source":
        return await memory.read_source(source or "", offset=offset)
    if type == "read":
        hits = await memory.search(MemoryQuery(terms=terms or ([value] if value else []), start=start, end=end))
        return (
            "\n".join(hit.describe() for hit in hits)
            or "No keyword matches. Try other words or a source reference; "
            "this does not establish that the experience never happened."
        )
    raise ValueError(f"Unknown memory operation: {type}")
