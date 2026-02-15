import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import aiofiles
import nonebot_plugin_localstore as store
from nonebot import logger
from pydantic import BaseModel, Field


@dataclass
class ConversationTurn:
    role: Literal["user", "muika", "system"]
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
    confidence: float = Field(..., ge=0, le=1, description="How important the memory is (0 to 1)")
    last_updated: datetime


class MemoryManager:
    def __init__(self, max_turns: int = 16):
        self.storage_path = store.get_plugin_data_dir() / "memory.json"

        self.recent_turns: deque[ConversationTurn] = deque(maxlen=max_turns)
        self.memory: dict[str, MemoryItem] = {}

    async def _save(self):
        """持久化记忆到磁盘"""
        # Ensure directory exists
        if not self.storage_path.parent.exists():
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "memory": {k: v.model_dump(mode="json") for k, v in self.memory.items()},
            # recent_turns 通常不需要持久化，或者只持久化最后几条用于热启动
        }
        async with aiofiles.open(self.storage_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(data, indent=2, ensure_ascii=False))

    async def load(self):
        """从磁盘加载记忆"""
        if not self.storage_path.exists():
            return
        try:
            async with aiofiles.open(self.storage_path, "r", encoding="utf-8") as f:
                content = await f.read()
                data = json.loads(content)
                for k, v in data.get("memory", {}).items():
                    self.memory[k] = MemoryItem(**v)
        except Exception as e:
            logger.error(f"Failed to load memory: {e}")

    def _build_key(self, category: str, key: str) -> str:
        return f"{category}:{key}"

    def add_context(self, role: Literal["user", "muika", "system"], content: str):
        """
        统一记录所有的交互历史：
        - user: 用户说的话
        - muika: AI 说的话
        - system: Action 的执行结果 (如 RSS 内容、文件内容、错误信息)
        """
        self.recent_turns.append(
            ConversationTurn(
                role=role,
                content=content,
                timestamp=datetime.now(),
            )
        )

    async def record_memory(self, intent: MemoryIntent):
        if intent.type == "noop":
            return

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
            logger.debug(f"Memory Updated: {key} = {intent.value}")

        elif intent.type == "forget":
            if key in self.memory:
                del self.memory[key]
                logger.debug(f"Memory Forgot: {key}")

        await self._save()

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
                prefix = {"user": "User", "muika": "You", "system": "System"}.get(turn.role, turn.role)
                parts.append(f"{prefix}: {turn.content}")

        return "\n".join(parts)
