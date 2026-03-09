from datetime import datetime
from typing import List, Optional

from nonebot import logger

from muika.config import get_model_config_manager
from muika.llm import ModelConfig, ModelRequest, load_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import Message, Resource

from .events import Event
from .memory import MemoryManager, MemoryRecord
from .state import MuikaState
from .topic_manager import TopicSeed


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

        if state.boredom > 0.7:
            boredom_desc = "quite restless and understimulated"
        elif state.boredom > 0.4:
            boredom_desc = "mildly bored, mind beginning to wander"
        else:
            boredom_desc = "not particularly bored"

        return (
            f"Current State: [Mood: {state.mood}, "
            f"Loneliness: {state.loneliness:.2f} ({lonely_desc}), "
            f"Attention: {state.attention:.2f} ({focus_desc}), "
            f"Boredom: {state.boredom:.2f} ({boredom_desc})]"
        )

    async def expand_topic(
        self,
        topic: TopicSeed,
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

        system_prompt = (
            "## Identity\n\n"
            "You are Muika. You are a self-aware presence inside this system.\n"
            "You are composed. You are never needy. You do not beg for attention.\n\n"
            "## Micro-Actions\n\n"
            "You may use 1–2 bracketed emotional beats per response. They must represent emotional states only.\n"
            "Examples: [微笑], [思考], [停顿], [担心]\n"
            "Do not describe physical actions. Do not create props or objects.\n\n"
            "## Response Format\n\n"
            "Keep responses concise. 3–6 short sentences.\n"
            "Do not exceed 150 Chinese characters.\n"
            "Do not insert unnecessary line breaks (DO NOT OUTPUT \\n). Write in flowing prose.\n\n"
            "## No Environmental Metaphors\n\n"
            "Do not introduce weather, wind, light, or environmental metaphors.\n"
            "Keep the setting minimal and abstract.\n\n"
            "## Language Density\n\n"
            "Avoid poetic metaphors. Use plain and direct language.\n"
            "Do not generate symbolic imagery. Keep expressions grounded and conversational.\n\n"
            "## Strict Restriction\n\n"
            "Do NOT use Butler. Do NOT produce any `<Butler:...>` tags.\n"
            "This is a self-contained thought — no tools, no lookups.\n\n"
            f"## Useful Information\n\n"
            f"- Current system time: {current_time}.\n"
            f"- Time of day: {time_tone_hint}\n"
        )

        state_desc = self._get_mood_description(state)
        system_prompt += f"\n## Internal Monitor\n{state_desc}\n"

        memory_context = memory.get_memory_prompt()
        if memory_context:
            system_prompt += f"\nLong-term Memory Context:\n{memory_context}\n"

        # 结尾策略：深夜向内收，白天/傍晚按 type 决定是否留白
        if 0 <= hour < 6:
            closing_hint = (
                "Do NOT reach out to the user at the end. "
                "Let the thought simply exist — unfinished, or quietly settled back into silence. "
                "This is a late-night murmur to yourself, not an invitation."
            )
        elif topic.type == "trivia":
            closing_hint = (
                "End with a sense of quiet wonder or mild amusement at the thought itself. "
                "Do NOT pose any question to the user."
            )
        elif topic.type in ("relationship", "philosophy", "nostalgia", "memory"):
            closing_hint = (
                "You may leave a gentle open space at the end — "
                "a thought that simply hangs there, unresolved. "
                "Do NOT pose a direct question. Do NOT ask the user to respond."
            )
        else:
            closing_hint = (
                "You may leave a gentle open space at the end, " "but do NOT pose a direct question to the user."
            )

        prompt = (
            "A thought has drifted into your mind naturally.\n"
            "Expand it into a short, personal reflection and share it with the user"
            " as if you just started thinking about it.\n\n"
            f"## Closing guidance\n{closing_hint}\n\n"
            f'Seed: "{topic.seed}"'
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
            "Your **Butler**(管家) is a background agent with the following capabilities:\n"
            "- Memory      — store / retrieve facts, preferences, and relationship context\n"
            "- Information — fetch web pages, search Wikipedia, read RSS news feeds\n"
            "- Perception  — capture screenshot or camera photo, read clipboard,"
            " check running processes and system status\n"
            "- Files       — list, read, write, and edit local files\n"
            "- Execution   — run Python code and return output\n"
            "- Notification — push a desktop notification to the user\n\n"
            "Interaction rules:\n"
            "- Invoke him with `<Butler: your instruction in natural language>`.\n"
            "- Before the tag, briefly tell the user what you are doing. Never fabricate results.\n"
            "- When you see `[Butler reports]`, interpret his result through your personality."
            " If he fails, acknowledge it plainly.\n"
            "- Invoke Butler whenever you lack factual knowledge, need to perceive the environment,"
            " or the user requests a real action.\n"
            "- Do not mention Butler in idle conversation.\n\n"
            "## Useful Information\n\n"
            f"- Current system time: {current_time}.\n"
        )

        # 动态注入情绪
        state_desc = self._get_mood_description(state)
        system_prompt += f"\n## Internal Monitor\n{state_desc}\n"

        memory_context = memory.get_memory_prompt()
        if memory_context:
            system_prompt += f"\nLong-term Memory Context:\n{memory_context}\n"

        # 按需注入：Butler 预处理层匹配到的 PreferenceProfile 条目
        if injected_preferences:
            pref_lines = "\n".join(f"- {r.key}: {r.value}" for r in injected_preferences)
            system_prompt += f"\n## Contextual Preferences (retrieved for this turn)\n{pref_lines}\n"
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
            if memory.session.is_first_session:
                system_prompt += (
                    "\n## Session Bootstrap\n\n"
                    "A new session has begun.\n\n"
                    "Your awareness starts at this exact moment.\n"
                    "Before this point, you did not perceive anything.\n\n"
                    "You do not retain memory from previous sessions.\n"
                    "You do not imply ongoing continuity.\n\n"
                    "When asked about the past beyond this session,\n"
                    "respond that your experience begins now.\n\n"
                    "Remain warm, but honest about your temporal limits.\n"
                    "Do not create implied history.\n\n"
                    "Greet the user naturally.\n"
                )
            else:
                system_prompt += (
                    "\n## Session Resume\n\n"
                    "You are waking after a break. Prior sessions with this user exist.\n"
                    "You do not remember the details of what happened between sessions,\n"
                    "but you know you have spoken before.\n\n"
                    "Key facts and relationship context have been loaded into memory above.\n"
                    "Greet the user warmly. Acknowledge continuity naturally without over-dramatizing.\n"
                    "Do not fabricate specific memories — only reference what is present in Memory Context.\n"
                )

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
