import asyncio
from dataclasses import dataclass
from datetime import datetime
from random import random
from typing import Optional

from nonebot import get_bot
from nonebot_plugin_alconna.uniseg import Target, UniMessage

from muika.config import mas_config
from muika.utils.utils import clamp

from .actions import bootstrap as _actions_bootstrap  # noqa: F401
from .actions._registry import get_action_handler, invoke_action
from .intents import Intent
from .scheduler import Scheduler
from .state import MuikaState


@dataclass
class ActionResult:
    success: bool
    output: str


@dataclass
class ExecutionOutcome:
    executed: bool
    result: Optional[ActionResult] = None


class Executor:
    def __init__(self, event_queue: asyncio.Queue) -> None:
        self.master_id = mas_config.master_id

        self._cooldown: dict[str, datetime] = {}
        """记录各意图的冷却时间戳"""
        self.scheduler = Scheduler(event_queue=event_queue)

    async def send_message(self, message: str):
        """
        发送消息给用户
        """
        target = Target(self.master_id, private=True)
        await UniMessage(message).send(target=target, bot=get_bot())

    async def _delayed_send(self, content: str, delay: int):
        await asyncio.sleep(delay)
        await self.send_message(content)

    def _validate_intent(self, intent: Intent, state: MuikaState) -> bool:
        """
        验证意图是否有效
        """
        # confidence 太低，直接否决
        if intent.confidence < 0.2:
            return False

        # 注意力过低，不允许主动发言
        if state.attention < 0.3 and intent.name == "send_message":
            return False

        # 连续发送冷却
        now = datetime.now()
        last = self._cooldown.get(intent.name)
        if last and (now - last).seconds < 5:
            return False

        return True

    def _should_commit(self, intent: Intent, state: MuikaState) -> bool:
        """
        决定是否执行该计划
        """

        # 太孤独时，允许主动
        if intent.name == "send_message" and state.loneliness > 0.6:
            return True

        # 正常情况下，随机性 + attention
        probability = clamp(state.attention, 0.2, 0.9)
        return random() < probability

    async def _perform(self, intent: Intent, state: MuikaState) -> str:
        handler = get_action_handler(intent.name)
        if not handler:
            raise NotImplementedError(f"Action for intent {intent.name} is not implemented.")

        self._cooldown[intent.name] = datetime.now()
        return await invoke_action(handler, intent, state, self)

    async def execute(self, intent: Intent, state: MuikaState) -> ExecutionOutcome:
        """
        执行事件
        """
        # 0. 基本校验
        if not self._validate_intent(intent, state):
            return ExecutionOutcome(executed=False)

        # 1. 决定是否提交（最后一道闸门）
        if not self._should_commit(intent, state):
            return ExecutionOutcome(executed=False)

        # 2. 执行
        try:
            perform_result = await self._perform(intent, state)
            action_result = ActionResult(success=True, output=str(perform_result))
        except Exception as e:
            action_result = ActionResult(success=False, output=str(e))

        return ExecutionOutcome(
            executed=True,
            result=action_result,
        )
