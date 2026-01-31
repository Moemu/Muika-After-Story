import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional

from nonebot import logger
from pydantic import BaseModel, Field

from .action import Intent, SendMessageIntent
from .events import Event


@dataclass
class ConversationTurn:
    role: Literal["user", "muika", "internal"]
    content: str
    timestamp: datetime


class MemoryIntent(BaseModel):
    type: Literal["remember", "forget", "noop"]
    category: Literal["user", "self", "world"]
    key: str
    value: Optional[str] = None
    strength: float = Field(..., ge=0, le=1)
    reason: Optional[str] = None


class MemoryItem(BaseModel):
    category: Literal["user", "self", "world"]
    key: str
    value: str
    confidence: float = Field(..., ge=0, le=1)
    last_updated: datetime


class MemoryManager:
    def __init__(self, max_turns: int = 16, storage_path: str | Path = "./data/muika_memory.json"):
        self.storage_path = Path(storage_path)

        self.recent_turns: deque[ConversationTurn] = deque(maxlen=max_turns)
        self.memory: dict[str, MemoryItem] = {}

    def _save(self):
        """持久化记忆到磁盘"""
        data = {
            "memory": {k: v.model_dump(mode="json") for k, v in self.memory.items()},
            # recent_turns 通常不需要持久化，或者只持久化最后几条用于热启动
        }
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _load(self):
        """从磁盘加载记忆"""
        if not self.storage_path.exists():
            return
        try:
            with open(self.storage_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in data.get("memory", {}).items():
                    self.memory[k] = MemoryItem(**v)
        except Exception as e:
            logger.error(f"Failed to load memory: {e}")

    def _build_key(self, category: str, key: str) -> str:
        return f"{category}:{key}"

    def record_event(self, event: Event) -> None:
        if event.type == "user_message":
            self.recent_turns.append(
                ConversationTurn(
                    role="user",
                    content=event.payload.message.message,
                    timestamp=event.timestamp,
                )
            )

    def record_intent(self, intent: Intent):
        if isinstance(intent, SendMessageIntent):
            self.recent_turns.append(
                ConversationTurn(
                    role="muika",
                    content=intent.content,
                    timestamp=datetime.now(),
                )
            )

    def record_memory(self, intent: MemoryIntent):
        key = self._build_key(intent.category, intent.key)
        if intent.type == "remember" and intent.value:
            # 只有 confidence 足够高才覆盖
            old_item = self.memory.get(key)
            if old_item and old_item.confidence > intent.strength:
                return  # 旧记忆更可靠，忽略新记忆

            self.memory[key] = MemoryItem(
                category=intent.category,
                key=intent.key,
                value=intent.value,
                confidence=intent.strength,
                last_updated=datetime.now(),
            )

        elif intent.type == "forget":
            if key in self.memory:
                del self.memory[key]

        self._save()

    def get_prompt_memory(self) -> str:
        """
        将 KV 记忆转化为自然语言 Prompt。
        为了防止 Token 爆炸，这里应该有一个筛选逻辑，或者按重要性排序。
        """
        parts = []

        sorted_memory = dict(sorted(self.memory.items(), key=lambda x: x[1].confidence, reverse=True))

        # 1. 核心事实 (User info)
        user_mems = [v for k, v in sorted_memory.items() if v.category == "user"]
        if user_mems:
            parts.append("## What you know about the User:")
            for mem in user_mems:
                parts.append(f"- {mem.key}: {mem.value}")

        # 2. 自我认知 (Self)
        self_mems = [v for k, v in sorted_memory.items() if v.category == "self"]
        if self_mems:
            parts.append("## What you know about yourself:")
            for mem in self_mems:
                parts.append(f"- {mem.key}: {mem.value}")

        # 3. 对话历史 (Short-term)
        if self.recent_turns:
            parts.append(
                "\n## Recent Context (Most recent at bottom): (Do NOT respond to these directly unless relevant)"
            )
            for turn in self.recent_turns:
                prefix = {"user": "User", "muika": "You", "system": "System", "internal": "My Inner Voice"}.get(
                    turn.role, turn.role
                )
                parts.append(f"{prefix}: {turn.content}")

        return "\n".join(parts)
