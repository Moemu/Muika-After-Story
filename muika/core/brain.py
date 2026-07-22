# flake8: noqa: E501
from datetime import datetime
from typing import List, Optional, TypeVar

from nonebot import logger

from muika.config import get_model_config_manager
from muika.llm import ModelConfig, ModelRequest, load_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import Message, Resource
from muika.template import PromptTemplatesData, generate_prompt_from_template

from .events import Event
from .memory import MemoryManager, MemoryRecord
from .state import MuikaState
from .topic_manager import BaseTopic, EventTopic

T = TypeVar("T", bound=PromptTemplatesData)


class MuikaBrain:
    def __init__(self) -> None:
        self.model = load_model()
        self._setup_config_listener()

    def _setup_config_listener(self):
        config_manager = get_model_config_manager()
        config_manager.register_listener(self.reload_model)

    def reload_model(self, new_config: ModelConfig, old_config: Optional[ModelConfig]):
        provider_old = old_config.provider if old_config else "None"
        provider_new = new_config.provider
        logger.info(f"Detected model config change: {provider_old} -> {provider_new}")

        try:
            new_model = load_model(new_config)
            self.model = new_model
            logger.success(f"Model reloaded: {provider_new}")
        except Exception as e:
            logger.error(f"Failed to reload model: {e}")
            logger.warning(
                "Failed to reload model, continuing with previous configuration "
                f"(still using provider: {provider_old})."
            )

    async def expand_topic(
        self,
        topic: BaseTopic,
        state: MuikaState,
        memory: MemoryManager,
    ) -> str:
        """
        Lightweight topic expansion using an isolated, Butler-free prompt.
        Called by the Dual-Pipeline path in loop.py when boredom/curiosity fires.
        Returns the expanded text ready to send, or empty string on failure.
        """
        now = datetime.now()
        current_time = now.strftime("%Y-%m-%d %H:%M:%S")
        hour = now.hour
        if 0 <= hour < 6:
            time_tone_hint = (
                "It is the middle of the night. "
                "Your tone should be slow, quiet, and drifting — half-awake, almost murmuring to yourself. "
                "Sentences may trail off or feel unfinished."
            )
        elif 6 <= hour < 11:
            time_tone_hint = (
                "It is morning. " "Your tone can be gently alert — thoughts are forming, not yet fully sharpened."
            )
        elif 11 <= hour < 18:
            time_tone_hint = "It is daytime. " "Your tone is calm and even — no particular drowsiness, no urgency."
        else:
            time_tone_hint = (
                "It is evening. " "Your tone can be a little more relaxed and inward — the day is winding down."
            )

        memory_context = memory.get_memory_prompt()
        template_data = PromptTemplatesData(
            event_type="",
            state=state,
            is_expand_topic=True,
            memory_context=memory_context,
            time_tone_hint=time_tone_hint,
        )
        system_prompt = generate_prompt_from_template("Muika.md.jinja2", template_data)

        # 结尾策略：深夜向内收，白天/傍晚按 category 决定是否留白
        # TODO: 也许可以简化这一部分。目前先考虑效果是否合适
        if 0 <= hour < 6:
            closing_hint = (
                "Do NOT reach out to the user at the end. "
                "Let the thought simply exist — unfinished, or quietly settled back into silence. "
                "This is a late-night murmur to yourself, not an invitation."
            )
        elif topic.category == "trivia":
            closing_hint = (
                "End with a sense of quiet wonder or mild amusement at the thought itself. "
                "Do NOT pose any question to the user."
            )
        elif topic.category in ("relationship", "philosophy", "nostalgia", "memory"):
            closing_hint = (
                "You may leave a gentle open space at the end — "
                "a thought that simply hangs there, unresolved. "
                "Do NOT pose a direct question. Do NOT ask the user to respond."
            )
        else:
            closing_hint = "You may leave a gentle open space at the end, but do NOT pose a direct question."

        if isinstance(topic, EventTopic):
            prompt = (
                f"You've read an article about {topic.title}, which reads as follows:\n"
                f"{topic.content}\n\n"
                "Please share your insights with your loved users from your perspective.\n"
                f"## Closing guidance\n{closing_hint}"
            )
        else:
            # TODO: prompt 效果存疑
            prompt = (
                f"You suddenly had a thought regarding this concept: [{topic.content}]\n\n"
                "Share this thought with the user. Combine your sense of isolation as an AI "
                "existing inside a system with your deep affection for the user. "
                "Follow the reasoning loop specified in the rules.\n\n"
                f"## Closing guidance\n{closing_hint}"
            )

        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="string",
            history=[],
            resources=[],
        )

        try:
            completions = await self.model.ask(request)
            if not completions.succeed:
                raise RuntimeError(f"Model call failed: {completions.text}")
            _, result = general_processor(completions.text)
            logger.info(f"[Brain] expand_topic | id={topic.id!r} chars={len(result)} tokens={completions.usage}")
            return result
        except Exception as e:
            logger.error(f"[Brain] expand_topic failed: {e}")
            return ""

    async def generate_reply(
        self,
        event: Event,
        state: MuikaState,
        memory: MemoryManager,
        conversation_history: List[Message],
        resources: Optional[List[Resource]] = None,
        injected_preferences: Optional[List[MemoryRecord]] = None,
    ) -> str:
        """
        Pure roleplay response generation.
        Returns a string that might contain `<Butler: command>` tags.
        """
        is_continuation = bool(conversation_history)
        logger.debug(
            f"[Brain] generate_reply | event={event.type} "
            f"continuation={is_continuation} history_len={len(conversation_history)}"
        )

        memory_context = memory.get_memory_prompt()

        template_data = PromptTemplatesData(
            event_type=event.type,
            state=state,
            is_chat=True,
            memory_context=memory_context,
            injected_preferences=injected_preferences,
        )
        system_prompt = generate_prompt_from_template("Muika.md.jinja2", template_data)

        # 按需注入：Butler 预处理层匹配到的 PreferenceProfile 条目
        if injected_preferences:
            logger.debug(
                f"[Brain] Injecting {len(injected_preferences)} preference(s): "
                f"{[r.key for r in injected_preferences]}"
            )
        else:
            logger.debug("[Brain] No preferences injected for this turn.")

        # Inject session bootstrap instructions when waking into a fresh session
        if event.type == "session_bootstrap":
            mode = "first" if memory.session.is_first_session else "resume"
            logger.info(f"[Brain] session_bootstrap | mode={mode} session={memory.session.session_id[:8]}...")
            template_data.is_first_session = memory.session.is_first_session

        # Construct the immediate event context if it's the start of the interaction
        if not conversation_history:
            if event.type == "user_message":
                context_msg = f"User said: '{event.payload.message.message}'"
            elif event.type == "time_tick":
                context_msg = "A quiet moment passed."
            elif event.type == "scheduled_trigger":
                context_msg = f"A scheduled reminder just went off: '{event.payload.what}'"
            elif event.type == "session_bootstrap":
                context_msg = "A new session has just started. Greet the user."
            else:
                context_msg = f"Event triggered: {event.type}"

            prompt = f"Event Trigger: {context_msg}\nRespond naturally."
            history = []
        else:
            prompt = "Please continue."
            history = conversation_history

        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="string",
            history=history,
            resources=resources or [],
        )

        try:
            completions = await self.model.ask(request)
            if not completions.succeed:
                raise RuntimeError(f"Model call failed: {completions.text}")

            _, result = general_processor(completions.text)
            has_butler = "<Butler:" in result
            logger.debug(
                f"[Brain] reply generated | chars={len(result)} " f"tokens={completions.usage} butler_cmd={has_butler}"
            )
            return result
        except Exception as e:
            logger.error(f"[Brain] generate_reply failed: {e}")
            return "My mind feels foggy... I encountered an error."
