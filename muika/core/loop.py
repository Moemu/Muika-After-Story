import asyncio
import time
from random import random
from typing import Optional

from nonebot import logger

from .brain import MuikaBrain
from .events import ActionFeedbackEvent, ActionFeedbackPayload, Event, TimeTickEvent
from .executor import Executor
from .intents import DoNothingIntent, Intent, Persistence
from .memory import MemoryManager
from .state import MuikaState

CURIOSITY_THRESHOLD = 0.6
CURIOSITY_DRIVE_INCREASE = 0.01


class Muika:
    def __init__(self) -> None:
        self.is_alive: bool = False
        self.curiosity_drive: float = 0.0
        """好奇驱动槽"""

        self.state = MuikaState()
        self.memory = MemoryManager()
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self.executor = Executor(self.event_queue)
        self.brain = MuikaBrain()

    async def collect_events(self) -> Event:
        """
        优先处理外部事件，如果没有外部事件，则产生 TimeTick
        """
        try:
            # 等待事件，超时则产生 TimeTick（心跳）
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event):
        """
        添加外部事件
        """
        await self.event_queue.put(event)

    def should_think(self, event: Event) -> bool:
        if event.type == "time_tick":
            if self.state.loneliness > 0.8:
                logger.debug("Trigger: Loneliness threshold breached.")
                return True
            if self.state.boredom > 0.6:
                logger.debug("Trigger: Boredom threshold breached.")
                return True
            # 随机闪念 (Random Thought)
            if self.curiosity_drive > CURIOSITY_THRESHOLD and random() < 0.3:
                logger.info("Trigger: Random thought occurred.")
                self.curiosity_drive = 0.0
                return True
            return False

        if isinstance(event, ActionFeedbackEvent):
            return event.payload.intent.name not in {"do_nothing", "send_message"}

        return True

    def should_execute(self, intent: Intent) -> bool:
        """
        决定是否执行当前意图
        """
        if isinstance(intent, DoNothingIntent):
            return False

        elif intent.confidence < 0.3:
            return False

        return True

    def _select_best_intent(self, intents: list[Intent]) -> Optional[Intent]:
        # 1. 先选 STICKY
        sticky = [i for i in intents if i.persistence == Persistence.STICKY]
        if sticky:
            return sticky[0]

        # 2. 再选 SHORT_TERM
        short = [i for i in intents if i.persistence == Persistence.SHORT_TERM]
        if short:
            return short[0]

        # 3. 最后 EPHEMERAL
        ephemeral = [i for i in intents if i.persistence == Persistence.EPHEMERAL]
        if ephemeral:
            return ephemeral[0]

        return None

    async def loop(self):
        last_tick_time = time.time()

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time
            # 1. Collect Events (获取事件或通过 TimeTick 心跳)
            logger.debug("Collecting events...")
            event = await self.collect_events()
            logger.debug(f"Event collected: {event.type}")
            self.memory.record_event(event)

            # 2. Update Internal State (情绪/状态更新)
            self.state.tick_state(event, dt)
            logger.debug(f"Internal state updated: {self.state}")

            # 3. Self Think (决策 - 关键逻辑)
            if self.should_think(event):
                intent = await self.brain.think(event, self.state, self.memory)
                if intent.action.name != "do_nothing" and intent.action.confidence > 0.3:
                    self.state.pending_intents.append(intent.action)
                if intent.memory and intent.memory.type != "noop":
                    await self.memory.record_memory(intent.memory)
                logger.debug(f"Intent created: {intent}")
            else:
                intent = None

            # 4. Decide & Execute Actions
            # Decide whether to execute an intent
            target_intent = self._select_best_intent(self.state.pending_intents)
            if target_intent:
                self.state.active_intent = target_intent
                logger.debug(f"Selected intent for execution: {target_intent}")

            if not self.state.active_intent or not self.should_execute(self.state.active_intent):
                logger.debug("No active intent to execute.")
                continue

            # Execute the intent
            current_intent = self.state.active_intent
            logger.info(f"Executing intent: {current_intent}")
            execute_result = await self.executor.execute(current_intent, self.state)
            if not execute_result.executed:
                logger.debug("Intent execution skipped.")
                continue

            if execute_result.result and execute_result.result.success:
                logger.success("Intent executed successfully.")
                self.memory.record_intent(current_intent)
                self.state.pending_intents.remove(current_intent)
                self.state.active_intent = None
            else:
                failed_reason = execute_result.result.output if execute_result.result else "Unknown error"
                logger.warning(f"Intent execution failed: {failed_reason}")
                current_intent.failure_count += 1
                if current_intent.failure_count >= 3:
                    logger.warning("Intent failed too many times, discarding.")
                    self.state.pending_intents.remove(current_intent)
                    self.state.active_intent = None

            action_feedback_event = ActionFeedbackEvent(
                payload=ActionFeedbackPayload(
                    intent=current_intent,
                    result=execute_result.result,
                )
            )
            self.state.last_executed_intent = current_intent
            await self.create_event(action_feedback_event)

            # 5. Sleep
            self.curiosity_drive += self.state.curiosity * CURIOSITY_DRIVE_INCREASE * dt
            await asyncio.sleep(0.2)

    async def start(self):
        if self.is_alive:
            return

        self.is_alive = True
        logger.info("Wake up...")
        await self.memory.load()
        await self.loop()
