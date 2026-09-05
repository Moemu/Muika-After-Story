from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from muika.core.memory import MemoryCategory, MemoryLayer, MemoryManager
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger


class MemoryParams(BaseModel):
    type: Literal["remember", "forget", "read"] = Field(
        ...,
        description=(
            "'remember': store a key-value fact; "
            "'forget': delete a stored key; "
            "'read': list stored memories (optionally filtered by category)."
        ),
    )
    category: MemoryCategory = Field(
        MemoryCategory.USER,
        description=(
            "Memory category: 'user' for user facts, 'self' for self-knowledge, "
            "'world' for world facts, 'relation' for relationship state."
        ),
    )
    layer: MemoryLayer = Field(
        MemoryLayer.PREFERENCE,
        description=(
            "Which memory layer to write to. Choose carefully:\n"
            "'core'= CoreIdentity. Use for stable, high-confidence facts that define who the user IS "
            "or critical relationship anchors. Always injected into every system prompt. "
            "Examples: user's preferred name/nickname, confirmed occupation, confirmed daily schedule, "
            "first conversation date, a firmly stated long-term preference.\n"
            "'state'= RelationshipState. Use for recent, time-sensitive context that matters "
            "only for the current resumption of conversation. Expires naturally. "
            "Examples: topic of last conversation, recent emotional tone, an unresolved question, "
            "a recent disagreement.\n"
            "'preference' = PreferenceProfile. Use for long-term soft preferences and lifestyle facts "
            "that are useful but NOT identity-defining. Retrieved on demand, not always injected. "
            "Examples: favourite music genre, preferred coffee type, hobbies, sleep habits.\n"
            "'archive'= ArchiveMemory. Reserved for session summaries - do NOT use directly.\n"
            "RULE: If in doubt between 'core' and 'preference', ask: "
            "'Would forgetting this change how I should address or understand this person fundamentally?' "
            "If yes -> 'core'. If no -> 'preference'."
        ),
    )
    key: Optional[str] = Field(
        None,
        description="Memory key, required for 'remember' and 'forget'.",
    )
    value: Optional[str] = Field(
        None,
        description="Memory value, required for 'remember'.",
    )


@on_function_call(
    "Read, write, or forget a fact in Muika's long-term memory.",
    params=MemoryParams,
)
async def memory(
    type: str,
    memory: MemoryManager,
    category: str = "user",
    layer: str = "preference",
    key: Optional[str] = None,
    value: Optional[str] = None,
) -> str:
    """读取、保存或删除指定层的长期记忆。"""

    mem_category = MemoryCategory(category)
    mem_layer = MemoryLayer(layer)

    if type == "read":
        mem = memory.records
        if not mem:
            return "No memories stored yet."
        lines = [
            f"[{v.layer.value}/{v.category.value}] {v.key}: {v.value}"
            for _, v in sorted(mem.items(), key=lambda x: x[1].layer.value)
            if mem_category is None or v.category == mem_category
        ]
        return "\n".join(lines) if lines else "No matching memories found."

    if type == "remember":
        if not key or value is None:
            return "'key' and 'value' are required for 'remember'."
        await memory.upsert_memory(
            layer=mem_layer,
            category=mem_category,
            key=key,
            value=value,
        )
        logger.info(f"[Memory] Saved [{mem_layer.value}/{mem_category.value}] {key} = {value!r}")
        return ""

    if type == "forget":
        if not key:
            return "'key' is required for 'forget'."
        await memory.forget_memory(layer=mem_layer, category=mem_category, key=key)
        logger.info(f"[Memory] Forgot [{mem_layer.value}/{mem_category.value}] {key}")
        return ""

    return f"Unknown memory operation: {type!r}"
