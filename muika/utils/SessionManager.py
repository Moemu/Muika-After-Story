import asyncio
from typing import Dict, List, Optional

from nonebot import logger
from nonebot.adapters import Event
from nonebot_plugin_alconna.uniseg import UniMessage, UniMsg

from ..config import plugin_config


class SessionManager:
    def __init__(self) -> None:
        self.sessions: Dict[str, List[UniMsg]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        self._timeout = plugin_config.input_timeout

    async def _put(self, sid: str, msg: UniMsg) -> None:
        async with self._lock:
            if sid not in self.sessions:
                self.sessions[sid] = []
            self.sessions[sid].append(msg)

    async def _get_messages_length(self, sid: str) -> int:
        async with self._lock:
            return len(self.sessions.get(sid, []))

    def merge_messages(self, sid: str) -> UniMessage:
        merged_message = UniMessage()

        for message in self.sessions.pop(sid, []):
            merged_message += message

        return merged_message

    async def put_and_wait(self, event: Event, message: UniMsg) -> Optional[UniMessage]:
        sid = event.get_session_id()
        await self._put(sid, message)

        old_length = await self._get_messages_length(sid)
        logger.debug(f"开始等待后续消息 ({self._timeout}s): 会话 {sid}, 当前消息数 {old_length}")
        await asyncio.sleep(self._timeout)
        new_length = await self._get_messages_length(sid)

        if new_length != old_length:
            logger.debug(f"发现新消息插入，当前处理器退出，会话 {sid} 交由后续处理器处理")
            return None

        logger.debug(f"无新消息，当前处理器接管会话 {sid}")
        return self.merge_messages(sid)
