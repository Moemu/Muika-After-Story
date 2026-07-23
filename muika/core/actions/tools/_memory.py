from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Optional

from pydantic import Field

from muika.core.memory import MemoryCategory, MemoryLayer
from muika.utils.logger import logger

from ..schema import ActionOutput
from ._base import BaseTool

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class MemoryTool(BaseTool):
    """
    Read, write, or forget a fact in Muika's long-term memory.
    (Unless explicitly specified, do not enable this tool without authorization,
    as it will directly interrupt the proxy loop without returning any data)
    """

    name: Literal["memory"] = "memory"
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

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        if state.memory is None:
            return ActionOutput(content="[MemoryTool] MemoryManager not available.")

        if self.type == "read":
            mem = state.memory.records
            if not mem:
                return ActionOutput(content="No memories stored yet.")
            lines = [
                f"[{v.layer.value}/{v.category.value}] {v.key}: {v.value}"
                for _, v in sorted(mem.items(), key=lambda x: x[1].layer.value)
                if self.category is None or v.category == self.category
            ]
            return ActionOutput(content="\n".join(lines) if lines else "No matching memories found.")

        if self.type == "remember":
            if not self.key or self.value is None:
                return ActionOutput(content="[MemoryTool] 'key' and 'value' are required for 'remember'.")
            await state.memory.upsert_memory(
                layer=self.layer,
                category=self.category,
                key=self.key,
                value=self.value,
            )
            logger.info(f"[MemoryTool] Saved [{self.layer.value}/{self.category.value}] {self.key} = {self.value!r}")
            return ActionOutput(
                content=f"Memory saved - [{self.layer.value}/{self.category.value}] {self.key} = {self.value!r}",
                silent=True,
            )

        if not self.key:
            return ActionOutput(content="[MemoryTool] 'key' is required for 'forget'.")

        await state.memory.forget_memory(layer=self.layer, category=self.category, key=self.key)
        logger.info(f"[MemoryTool] Forgot [{self.layer.value}/{self.category.value}] {self.key}")
        return ActionOutput(
            content=f"Memory forgotten - [{self.layer.value}/{self.category.value}] {self.key}",
            silent=True,
        )
