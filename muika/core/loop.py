import asyncio
import re
import time
from random import random

from nonebot import logger

from muika.models import Message

from .brain import MuikaBrain
from .butler.agent import ButlerAgent
from .events import Event, TimeTickEvent
from .executor import Executor
from .memory import MemoryManager
from .state import MuikaState

CURIOSITY_THRESHOLD = 0.6


class Muika:
    def __init__(self) -> None:
        self.is_alive: bool = False
        self.curiosity_drive: float = 0.0

        self.state = MuikaState()
        self.memory = MemoryManager()
        self.state.memory = self.memory  # 注入 memory 引用，供工具使用
        self.event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self.executor = Executor(self.event_queue)

        # New "Ojou-sama & Butler" Architecture
        self.brain = MuikaBrain()
        self.butler_agent = ButlerAgent()

    async def collect_events(self) -> Event:
        try:
            return await asyncio.wait_for(self.event_queue.get(), timeout=5.0)
        except asyncio.TimeoutError:
            return TimeTickEvent()

    async def create_event(self, event: Event):
        await self.event_queue.put(event)

    def should_think(self, event: Event) -> bool:
        if event.type == "time_tick":
            if self.state.loneliness > 0.8:
                logger.debug("TimeTick: loneliness threshold breached — will think.")
                return True
            if self.state.boredom > 0.6:
                logger.debug("TimeTick: boredom threshold breached — will think.")
                return True
            if self.curiosity_drive > CURIOSITY_THRESHOLD and random() < 0.3:
                self.curiosity_drive = 0.0
                logger.debug("TimeTick: curiosity drive fired — will think.")
                return True
            return False
        return True

    async def loop(self):
        last_tick_time = time.time()

        # We need to maintain a short-term inner dialogue context if a conversation spans multiple Butler calls
        inner_conversation_context = []

        while self.is_alive:
            current_time = time.time()
            dt = current_time - last_tick_time
            last_tick_time = current_time

            logger.debug("Collecting events...")
            event = await self.collect_events()

            if event.type == "time_tick" and not self.should_think(event):
                # Idle tick update
                self.state.tick_state(event, dt)
                continue

            if event.type == "user_message":
                logger.info(f"[Event] user_message | content: {event.payload.message.message!r}")
                self.memory.add_context("user", event.payload.message.message)
            elif event.type == "scheduled_trigger":
                logger.info(f"[Event] scheduled_trigger | what: {event.payload.what!r}")
            else:
                logger.info(f"[Event] {event.type}")

            self.state.tick_state(event, dt)
            logger.debug(
                f"[State] mood={self.state.mood} "
                f"loneliness={self.state.loneliness:.2f} "
                f"boredom={self.state.boredom:.2f} "
                f"attention={self.state.attention:.2f}"
            )

            inner_conversation_context.clear()

            # Ojou-sama conversational loop (Iterative Agent)
            max_inner_loops = 4
            for loop_idx in range(max_inner_loops):
                logger.debug(
                    f"[Brain] turn {loop_idx + 1}/{max_inner_loops} | history_len={len(inner_conversation_context)}"
                )

                # 1. Ask Ojou-sama
                reply = await self.brain.generate_reply(
                    event=event,
                    state=self.state,
                    memory=self.memory,
                    conversation_history=inner_conversation_context,
                )

                # 2. Append her raw reply to the inner conversation context
                inner_conversation_context.append(Message(message=reply, userid="Muika", profile="self"))

                # 3. Intercept Butler commands — format: <Butler: command>
                butler_commands = re.findall(r"<Butler:\s*(.+?)>", reply, re.DOTALL)
                clean_reply = re.sub(r"<Butler:\s*(.+?)>", "", reply, flags=re.DOTALL).strip()

                if butler_commands:
                    logger.info(f"[Brain] intercepted {len(butler_commands)} butler command(s)")

                if clean_reply:
                    logger.info(f"[Muika → User] {clean_reply!r}")
                    await self.executor.send_message(clean_reply)
                    self.memory.add_context("muika", clean_reply)

                if not butler_commands:
                    # No butler command — conversation turn is complete
                    logger.debug("[Brain] no butler commands, turn complete.")
                    break

                # 4. Butler executes each command and feeds results back
                for cmd in butler_commands:
                    logger.info(f"[Butler ←] {cmd!r}")
                    butler_report: str = await self.butler_agent.execute_command(cmd, self.state, self.executor)
                    logger.info(f"[Butler →] {butler_report!r}")
                    observation = f"[Butler reports]: {butler_report}"
                    inner_conversation_context.append(Message(message=observation, userid="System", profile="self"))
            else:
                logger.warning(
                    f"[Brain] reached max inner loops ({max_inner_loops}) without completing — possible butler loop."
                )

    def start(self):
        logger.info("Muika is waking up...")
        self.is_alive = True
        asyncio.create_task(self.loop())

    def stop(self):
        logger.info("Muika is going to sleep.")
        self.is_alive = False
