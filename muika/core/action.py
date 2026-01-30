import asyncio
from dataclasses import dataclass
from datetime import datetime
from random import random
from typing import Annotated, Any, Literal, Optional, TypeAlias, Union

from nonebot import get_bot
from nonebot_plugin_alconna.uniseg import Target, UniMessage
from pydantic import BaseModel, Field

from muika.config import mas_config
from muika.utils.utils import clamp

from .state import MuikaState

ACTION_DEFAULT_TTL = {
    "send_message": 1,
    "delay_message": 3600,
    "check_rss_update": 10,
}


class SendMessageIntent(BaseModel):
    name: Literal["send_message"] = "send_message"
    confidence: float
    reason: Optional[str] = None
    content: str


class DoNothingIntent(BaseModel):
    name: Literal["do_nothing"] = "do_nothing"
    confidence: float
    reason: Optional[str] = None


class DelayMessageIntent(BaseModel):
    name: Literal["delay_message"] = "delay_message"
    confidence: float
    reason: Optional[str] = None
    content: str
    target_time: datetime = Field(
        description="The planned time to send the message. Use a valid datetime format, e.g., '2024-12-31 23:59:59'."
    )


class CheckRSSUpdateIntent(BaseModel):
    name: Literal["check_rss_update"] = "check_rss_update"
    confidence: float
    reason: Optional[str] = None
    rss_source: str


Intent: TypeAlias = Annotated[
    Union[SendMessageIntent, DoNothingIntent, DelayMessageIntent, CheckRSSUpdateIntent], Field(discriminator="name")
]


@dataclass
class ActionPlan:
    name: str
    payload: dict[str, Any]
    ttl: int


class Executor:
    def __init__(self) -> None:
        self.master_id = mas_config.master_id

        self._cooldown: dict[str, datetime] = {}
        """记录各意图的冷却时间戳"""

    async def send_message(self, message: str):
        """
        发送消息给用户
        """
        target = Target(self.master_id)
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

    def _make_plan(self, intent: Intent, state: MuikaState) -> Optional[ActionPlan]:
        """
        生成执行计划
        """
        ttl = ACTION_DEFAULT_TTL.get(intent.name, 1)

        if isinstance(intent, SendMessageIntent):
            return ActionPlan(
                name="send_message",
                payload={"content": intent.content},
                ttl=ttl,
            )

        if isinstance(intent, DelayMessageIntent):
            delay = (intent.target_time - datetime.now()).seconds

            return ActionPlan(
                name="delay_message",
                payload={
                    "content": intent.content,
                    "delay": clamp(delay, 10, 86400),  # 86400 = 24 hours
                },
                ttl=ttl,
            )

        if isinstance(intent, CheckRSSUpdateIntent):
            return ActionPlan(
                name="check_rss_update",
                payload={"source": intent.rss_source},
                ttl=ttl,
            )

        return None

    def _should_commit(self, plan: ActionPlan, state: MuikaState) -> bool:
        """
        决定是否执行该计划
        """

        # 太孤独时，允许主动
        if plan.name == "send_message" and state.loneliness > 0.6:
            return True

        # 正常情况下，随机性 + attention
        probability = clamp(state.attention, 0.2, 0.9)
        return random() < probability

    async def _perform(self, plan: ActionPlan, state: MuikaState) -> None:
        self._cooldown[plan.name] = datetime.now()

        if plan.name == "send_message":
            await self.send_message(plan.payload["content"])

            # 行为反作用
            state.loneliness *= 0.7
            state.attention = min(1.0, state.attention + 0.1)

        elif plan.name == "delay_message":
            asyncio.create_task(
                self._delayed_send(
                    plan.payload["content"],
                    plan.payload["delay"],
                )
            )

        elif plan.name == "check_rss_update":
            ...  # TODO

    async def execute(self, intent: Intent, state: MuikaState) -> None:
        """
        执行事件
        """
        # 0. 基本校验
        if not self._validate_intent(intent, state):
            return

        # 1. 生成 ActionPlan
        plan = self._make_plan(intent, state)
        if not plan:
            return

        # 2. 决定是否提交（最后一道闸门）
        if not self._should_commit(plan, state):
            return

        # 3. 执行
        await self._perform(plan, state)
