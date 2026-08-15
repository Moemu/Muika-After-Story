# flake8: noqa: E501
from datetime import datetime
from typing import List, Optional, TypeVar

from muika.config import get_model_config_manager, mas_config
from muika.ipc.server import AdapterInfo
from muika.llm import ModelConfig, ModelRequest, load_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import Resource
from muika.plugin.func_call import get_function_list
from muika.template import (
    PromptTemplatesData,
    generate_prompt_from_template,
)
from muika.utils.logger import logger
from muika.utils.utils import format_duration

from .butler.agent import ENABLE_MCP
from .events import Event
from .memory import MemoryManager, MemoryRecord
from .state import MuikaState
from .topic_manager import BaseTopic, EventTopic

T = TypeVar("T", bound=PromptTemplatesData)


class MuikaBrain:
    def __init__(self) -> None:  # pragma: no cover
        self.model = load_model()
        self._mcp_tools: list[dict] = []
        self._setup_config_listener()

    async def _get_tool_list(self) -> list[dict]:  # pragma: no cover
        """组装 Muika 直接调用的完整工具列表（内置注册工具 + MCP，若启用）。"""
        tools = get_function_list()
        if ENABLE_MCP and not self._mcp_tools:
            from muika.plugin.mcp import get_mcp_list

            self._mcp_tools = await get_mcp_list()
            tools += self._mcp_tools
        return tools

    def _setup_config_listener(self):  # pragma: no cover
        config_manager = get_model_config_manager()
        config_manager.register_listener(self.reload_model)

    def reload_model(self, new_config: ModelConfig, old_config: Optional[ModelConfig]):  # pragma: no cover
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

    @staticmethod
    def generate_adapters_info(adapters: Optional[List[AdapterInfo]] = None) -> Optional[str]:
        """
        格式化 adapters 信息

        注意为了简化下游实现，当 len(adapters) < 2 时将返回空信息
        """
        if not adapters or len(adapters) < 2:
            return None

        adapters_infos: list[str] = []
        now = datetime.now()

        for adapter in adapters:
            delta = (now - adapter.last_active_at).total_seconds()
            if delta < 60:
                ago = "just now"
            elif delta < 3600:
                ago = f"{int(delta / 60)} min ago"
            elif delta < 86400:
                ago = f"{int(delta / 3600)} hours ago"
            else:
                ago = f"{int(delta / 86400)} days ago"

            adapters_infos.append(f"{adapter.client_name}(Last active at {ago})")

        return "\n".join(adapters_infos)

    async def expand_topic(
        self,
        topic: BaseTopic,
        state: MuikaState,
        memory: MemoryManager,
        adapters: Optional[List[AdapterInfo]] = None,
    ) -> str:
        """
        Lightweight topic expansion using an isolated, Butler-free prompt.
        Called by the Dual-Pipeline path in loop.py when boredom/curiosity fires.
        Returns the expanded text ready to send, or empty string on failure.
        """
        now = datetime.now()
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
            adapters_info=self.generate_adapters_info(adapters),
        )
        system_prompt = generate_prompt_from_template(mas_config.persona_template, template_data)

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
            logger.info(
                f"[Brain] expand_topic | id={topic.id!r} chars={len(result)} "
                f"tokens={completions.usage.total_tokens}"
            )
            return result
        except Exception as e:
            logger.error(f"[Brain] expand_topic failed: {e}")
            return ""

    async def generate_reply(
        self,
        event: Event,
        state: MuikaState,
        memory: MemoryManager,
        resources: Optional[List[Resource]] = None,
        injected_preferences: Optional[List[MemoryRecord]] = None,
        adapters: Optional[List[AdapterInfo]] = None,
        god_mode: bool = False,
    ) -> str:
        """
        Pure roleplay response generation.

        Returns a string that might contain ``<agent>...</agent>`` or ``<target: name>`` tags.
        """
        is_continuation = bool(memory.recent_turns)
        logger.debug(
            f"[Brain] generate_reply | event={event.type} "
            f"continuation={is_continuation} history_len={len(memory.recent_turns)}"
        )

        memory_context = memory.get_memory_prompt()

        template_data = PromptTemplatesData(
            event_type=event.type,
            state=state,
            is_chat=True,
            memory_context=memory_context,
            injected_preferences=injected_preferences,
            adapters_info=self.generate_adapters_info(adapters),
        )

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
            template_data.absence_bucket = event.absence_bucket
            template_data.last_connection_time = (
                event.last_chat_time.strftime("%Y-%m-%d %H:%M:%S") if event.last_chat_time else None
            )

        # Construct the immediate event context if it's the start of the interaction
        if event.type == "user_message":
            prompt = f"[User] {event.payload.message.message}"
        elif event.type == "time_tick":
            if state.mood == "lonely":
                prompt = (
                    "[System] A quiet moment passed, but the loneliness lingers. "
                    "You need to use some means to attract users' attention."
                )
            elif state.mood == "bored":
                prompt = (
                    "[System] A quiet moment passed, but the boredom persists. "
                    "Perhaps you can actively explore the user's computer or let your Agent alter-ego fetch some news"
                )
            else:
                prompt = "[System] A quiet moment passed."
        elif event.type == "scheduled_trigger":
            prompt = f"[System] A scheduled reminder just went off: '{event.payload.what}'"
        elif event.type == "timeout":
            wait = format_duration(event.duration)
            elapsed = format_duration((datetime.now() - event.set_at).total_seconds())
            prompt = (
                f"[System] The wait you set for the user's reply ({wait}) has passed — "
                f"you have been waiting for about {elapsed} now. Consider reminding the user, or seeing what they are doing?"
            )
        elif event.type == "session_bootstrap":
            prompt = "[System] A new session has just started. Greet the user."
        elif event.type == "adapter_online":
            prompt = (
                f"[System] The user just connected a new chat platform adapter for you: {event.adapter}, "
                "perhaps you can try chatting with the user on this platform"
            )
        else:
            prompt = f"[System] Event triggered: {event.type}"

        # 内化当前时间：每轮 prompt 统一带时间戳前缀，替代 system 中的 current_time，
        # 使 system prompt 保持字节级稳定以利前缀缓存。
        prompt = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {prompt}"

        # 历史记录去重
        history = memory.recent_turns.copy()
        if history:
            item = history[-1]
            if item.role == "user" and event.type == "user_message":
                user_msg = event.payload.message.message
                if user_msg == item.content:
                    history.pop()

        system_prompt = generate_prompt_from_template(mas_config.persona_template, template_data)

        tools = await self._get_tool_list() if god_mode else None
        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="string",
            history=history,
            resources=resources or [],
            tools=tools,
        )

        try:
            completions = await self.model.ask(request)
            if not completions.succeed:
                raise RuntimeError(f"Model call failed: {completions.text}")

            _, result = general_processor(completions.text)
            has_agent = "<agent>" in result.lower()
            logger.debug(
                f"[Brain] reply generated | chars={len(result)} "
                f"tokens={completions.usage.total_tokens} agent_cmd={has_agent}"
            )
            return result
        except Exception as e:
            logger.error(f"[Brain] generate_reply failed: {e}")
            return "My mind feels foggy... I encountered an error."
