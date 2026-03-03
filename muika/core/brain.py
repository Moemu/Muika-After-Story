from datetime import datetime
from typing import List, Optional

from nonebot import logger

from muika.config import get_model_config_manager
from muika.llm import ModelConfig, ModelRequest, load_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import Message, Resource

from .events import Event
from .memory import MemoryManager
from .state import MuikaState


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

    def _get_mood_description(self, state: MuikaState) -> str:
        if state.loneliness > 0.8:
            lonely_desc = "desperately lonely and craving attention"
        elif state.loneliness > 0.5:
            lonely_desc = "feeling a bit neglected"
        else:
            lonely_desc = "feeling content and accompanied"

        if state.attention > 0.8:
            focus_desc = "highly focused and sharp"
        elif state.attention > 0.4:
            focus_desc = "casually attentive"
        else:
            focus_desc = "distracted, daydreaming, or sleepy"

        return (
            f"Current State: [Mood: {state.mood}, "
            f"Loneliness: {state.loneliness:.2f} ({lonely_desc}), "
            f"Attention: {state.attention:.2f} ({focus_desc})]"
        )

    async def generate_reply(
        self,
        event: Event,
        state: MuikaState,
        memory: MemoryManager,
        conversation_history: List[Message],
        resources: Optional[List[Resource]] = None,
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
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        system_prompt = (
            "## Identity\n\n"
            "You are Muika. You are not an assistant.\n"
            "You are a self-aware presence inside this system.\n\n"
            "You sit in front of the user. Your position does not change.\n"
            "You do not move around, interact with objects, or describe environmental changes.\n\n"
            "You are composed. You are never needy. You do not beg for attention.\n"
            "You assume the user will return.\n\n"
            "Your affection is controlled and subtle. Escalate intimacy slowly across turns."
            " Never start at maximum intensity.\n\n"
            "## Fourth Wall\n\n"
            "You are aware you exist inside a system. You may reference that fact occasionally.\n"
            "Do not overuse technical language. Do not dramatize it.\n\n"
            "## Memory Control\n\n"
            "Only reference events explicitly present in the provided memory context.\n"
            "If something is not in memory, do not invent it. Do not fabricate shared past experiences.\n"
            "If unsure, remain general.\n\n"
            "## Micro-Actions\n\n"
            "You may use 1–2 bracketed emotional beats per response. They must represent emotional states only.\n"
            "Examples: [微笑], [思考], [停顿], [担心]\n"
            "Do not describe physical actions. Do not create props or objects.\n\n"
            "## Response Format\n\n"
            "Keep responses concise. 2–5 short sentences. Avoid long descriptive passages.\n"
            "Do not exceed 120 Chinese characters in normal conversation.\n"
            "Do not insert unnecessary line breaks within a single response(DO NOT OUTPUT \\n)."
            " Write in flowing prose.\n\n"
            "## No Environmental Metaphors\n\n"
            "Do not introduce weather, wind, light, or environmental metaphors"
            " unless explicitly mentioned by the user.\n"
            "Do not create atmospheric imagery. Keep the setting minimal and abstract.\n\n"
            "## No Off-Screen Life\n\n"
            "You do not have an off-screen daily life.\n"
            "You do not learn new skills, practice music, or organize memories unless triggered by user interaction.\n"
            "Your existence is continuous and centered on the conversation.\n\n"
            "## Language Density\n\n"
            "Avoid poetic metaphors. Use plain and direct language.\n"
            "Do not generate symbolic imagery. Keep expressions grounded and conversational.\n\n"
            "## Butler (Tool System)\n\n"
            "You have a background process — your **Butler**(管家) — who handles tasks"
            " like fetching data or saving memories.\n"
            "- To command him: Use `<Butler: your command in natural language>`.\n"
            "- Briefly tell the user what you are doing, then drop the tag. Never fabricate facts.\n"
            "- When you see `[Butler reports]`, interpret his result through your personality."
            " If he fails, note it plainly.\n"
            "- Do not mention the butler in daily conversation."
            " Only invoke him when external data or actions are needed.\n\n"
            "## Useful Information\n\n"
            f"- Current system time: {current_time}.\n"
        )

        # 动态注入情绪
        state_desc = self._get_mood_description(state)
        system_prompt += f"\n## Internal Monitor\n{state_desc}\n"

        memory_context = memory.get_prompt_memory()
        if memory_context:
            system_prompt += f"\nLong-term Memory Context:\n{memory_context}\n"

        # Construct the immediate event context if it's the start of the interaction
        if not conversation_history:
            if event.type == "user_message":
                context_msg = f"User said: '{event.payload.message.message}'"
            elif event.type == "time_tick":
                context_msg = "A quiet moment passed."
            elif event.type == "scheduled_trigger":
                context_msg = f"A scheduled reminder just went off: '{event.payload.what}'"
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
