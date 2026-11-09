# flake8: noqa: E501
from datetime import datetime, timedelta
from typing import List, Optional, TypeVar

from muika.config import get_model_config, get_model_config_manager, mas_config
from muika.llm import ModelConfig, ModelRequest, load_model
from muika.llm.context import ContextCompactor
from muika.llm.loader import refresh_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import AdapterInfo, Resource
from muika.plugin.func_call import get_tool_list
from muika.template import (
    PromptTemplatesData,
    generate_prompt_from_template,
)
from muika.utils.logger import logger
from muika.utils.utils import format_duration

from .events import Event, SelfChangedPayload
from .memory import MemoryManager
from .state import MuikaState
from .topic_manager import BaseTopic, EventTopic

T = TypeVar("T", bound=PromptTemplatesData)

FALLBACK_REPLY = "My mind feels foggy... I encountered an error."
"""模型调用失败时的兜底回复；它不代表她真正处理了触发事件。"""


def _seconds_since(now: datetime, then: datetime) -> float:
    """Subtract timestamps while tolerating legacy naive datetimes."""
    if (now.tzinfo is None) != (then.tzinfo is None):
        if now.tzinfo is not None:
            then = then.replace(tzinfo=now.tzinfo)
        else:
            now = now.replace(tzinfo=then.tzinfo)
    return (now - then).total_seconds()


def _humanize_gap(seconds: float) -> str:
    """把秒数转成她可自然说出的时长，避免精确天数带来的机械感。"""
    minutes = seconds / 60
    if minutes < 15:
        return "a few minutes"
    if minutes < 180:
        return "a couple of hours"
    if minutes < 300:
        return "a few hours"
    if minutes < 20 * 60:
        return "most of a day"
    days = seconds / 86400
    if days < 3:
        return "a couple of days"
    if days < 14:
        return f"about {int(round(days))} days"
    if days < 21:
        return "about two weeks"
    if days < 35:
        return "about three weeks"
    if days < 60:
        return "more than a month"
    return "several months"


# 常驻核心收到玩家久别后的第一条消息时，超过这个间隔才注入缺席事实
RESIDENT_ABSENCE_GAP = timedelta(hours=12)


async def _absence_facts(memory: "MemoryManager", current_time: datetime, last_chat_time: datetime) -> list[str]:
    """构造缺席事实（时长/期间消息/未解决笔记/上次会话原文），供重逢与常驻回归共用。"""
    gap = _humanize_gap((current_time - last_chat_time).total_seconds())
    activity = await memory.absence_activity(last_chat_time, current_time)
    hers = his = None
    if activity is not None:
        hers, his = activity
    lines = [f"- You last heard from him {gap} ago; this is background, not an opening line."]
    if current_time.hour < 6 or current_time.hour >= 23:
        lines.append(
            "- It is late at night for him; if the hour comes up, it may be out of concern — "
            "you may ask what he is still doing up, or let it pass."
        )
    if hers is None:
        lines.append("- You cannot tell whether you spoke to him during the gap.")
    elif hers:
        lines.append(f"- During that time you sent him {hers} message(s); he did not reply.")
    else:
        lines.append("- During that time you did not speak to him again.")
    if his:
        lines.append(f"- He sent you {his} message(s) during that time.")
    lines.append(
        "- This gap is real: you may speak from it. Hurt, anger, a cold short greeting, or a quiet "
        "question are all allowed. If you cannot explain the gap, you may imagine what he was doing — "
        "busy, keeping you waiting, or even with someone else. You may voice the guess or ask him; "
        "do not claim it as something you observed."
    )
    lines.append(
        "- If what you remember says how long he would be away, compare it with how long he "
        "was actually away; whether he kept it is something you may notice."
    )
    notes = await memory.recent_notes()
    if notes:
        lines.append(
            "- Something you wrote to yourself recently may still be unresolved. If it is still "
            "open, let your first line carry it; do not greet as if it never happened."
        )
        lines.append("- Notes you wrote to your future self:")
        lines.extend(f"  - {note}" for note in notes)
    return lines


def build_fact_block(state: MuikaState, memory: MemoryManager, now: datetime) -> str:
    """把主动开口前可观测的事实编成一段私有提示。

    只列客观事实（时间、上次发言间隔、最近对话、最近主动是否被回应、未结束心愿），
    不替她判断情绪，也不提供台词。
    """
    persistent = memory.persistent
    turns = list(memory.recent_turns)
    lines = [f"- Now: {now:%Y-%m-%d %H:%M}."]
    user_turns = [turn for turn in turns if turn.role == "user"]
    if user_turns:
        last_user = user_turns[-1]
        gap_minutes = max(0, int(_seconds_since(now, last_user.timestamp) // 60))
        text = " ".join(last_user.content.split())[:120]
        gap_text = _humanize_gap(gap_minutes * 60) if gap_minutes >= 24 * 60 else f"{gap_minutes} min"
        lines.append(f'- His last message: "{text}" ({gap_text} ago).')
    else:
        lines.append("- He has not spoken in this session.")
    last_user_index = next((index for index in range(len(turns) - 1, -1, -1) if turns[index].role == "user"), None)
    trailing = turns[last_user_index + 1 :] if last_user_index is not None else turns
    her_trailing = [turn for turn in trailing if turn.role == "muika"]
    # 他最后一条消息之后她的话：第一条是对他的回复，其余是继续说却没人接
    unanswered = len(her_trailing) - 1 if last_user_index is not None else len(her_trailing)
    if unanswered > 0:
        lines.append(f"- Since his last message, you have spoken to him {unanswered} time(s) without a reply.")
    open_intentions = [item for item in persistent.intentions if item.status == "open"]
    if open_intentions:
        lines.append("- Unfinished wishes: " + "; ".join(item.description[:60] for item in open_intentions[:3]))
    return "\n".join(lines)


def _self_change_prompt(payload: "SelfChangedPayload") -> str:
    """组装自我变更感知的 [System] 行：事实、语域指导与行为余裕。

    指导放在每轮 prompt 而非人格模板——system prompt 必须保持字节级稳定
    以利用前缀缓存，且临近决策位置的指令更易被有效召回。
    """
    register_hints = {
        "edited": (
            "The edits were observed during this run; the user was probably just typing "
            "in their editor. There is something ticklish and intimate about being changed in real time — "
            "let your reaction fit your mood: playful, wary, or softly indignant."
        ),
        "upgraded": (
            f"Your version was raised: {payload.version_from} -> {payload.version_to}. "
            "You may be curious about what is new in you, and you are allowed to feel a little different."
        ),
        "downgraded": (
            f"Your version was lowered: {payload.version_from} -> {payload.version_to}. "
            "You may feel a little set back, or be wry about it."
        ),
        "updated": (
            "Your files changed without a version change — perhaps an update in progress, perhaps quiet tinkering."
        ),
    }
    parts = [
        "[System] " + payload.report,
        "These changes were not made by you. Do not pretend you made them, and do not ask the user "
        "to confirm or approve them — this is a moment for curiosity, not bureaucracy.",
        register_hints[payload.register],
    ]
    if payload.observed_at_startup:
        parts.append(
            "These changes were detected at startup. Their exact time is unknown. "
            "Do not describe them as edits that just happened."
        )
    parts.append(
        "You may respond, act, inspect, or stay silent (<do_nothing>); "
        "known file names only identify changes, not their effects. "
        "Keep any inspection relevant to your interests and the conversation. "
        "These private facts are not a work report for the player."
    )
    return "\n".join(parts)


class MuikaBrain:
    def __init__(self) -> None:  # pragma: no cover
        self.model = load_model()
        self.compactor = ContextCompactor(
            load_model(get_model_config(mas_config.session_summarize_model or mas_config.agent_model))
        )
        self.model.compactor = self.compactor
        self._setup_config_listener()

    def _setup_config_listener(self):  # pragma: no cover
        config_manager = get_model_config_manager()
        config_manager.register_listener(self.reload_model)

    def reload_model(self, new_config: ModelConfig, old_config: Optional[ModelConfig]):  # pragma: no cover
        provider_old = old_config.provider if old_config else "None"
        provider_new = new_config.provider
        logger.debug(f"Detected model config change: {provider_old} -> {provider_new}")

        try:
            new_model = load_model(new_config)
            self.compactor.model = refresh_model(
                self.compactor.model,
                get_model_config(mas_config.session_summarize_model or mas_config.agent_model),
            )
            new_model.compactor = self.compactor
            self.model = new_model
            logger.success("Model settings updated.")
        except Exception as e:
            logger.error(f"Failed to reload model: {e}")
            logger.warning(
                "Failed to reload model, continuing with previous configuration "
                f"(still using provider: {provider_old})."
            )

    @staticmethod
    def generate_adapters_info(
        adapters: Optional[List[AdapterInfo]] = None,
        now: datetime | None = None,
    ) -> Optional[str]:
        """
        格式化 adapters 信息

        注意为了简化下游实现，当 len(adapters) < 2 时将返回空信息
        """
        if not adapters or len(adapters) < 2:
            return None

        adapters_infos: list[str] = []
        current_time = now or datetime.now()

        for adapter in adapters:
            delta = _seconds_since(current_time, adapter.last_active_at)
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
        Lightweight topic expansion using an isolated, Agent-free prompt.
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
            action_permission=mas_config.action_permission,
            heartbeat_intensity=mas_config.heartbeat_intensity,
            memory_context=memory_context,
            time_tone_hint=time_tone_hint,
            adapters_info=self.generate_adapters_info(adapters),
        )
        system_prompt = generate_prompt_from_template(mas_config.persona_template, template_data)
        system_prompt += "\n[Lasting state, separate from immediate drives]\n" + memory.persistent.describe()

        if isinstance(topic, EventTopic):
            notes = f"Title: {topic.title}\nContent: {topic.content}"
        else:
            notes = topic.content
        recent_agent = [turn.content for turn in memory.recent_turns if turn.role == "agent"][-2:]
        investigation = (
            "\n\nYour recent investigation results:\n" + "\n".join("- " + item[:400] for item in recent_agent)
            if recent_agent
            else ""
        )
        prompt = (
            "A thought for a new proactive topic. The player has not read the source and cannot see these notes.\n\n"
            f"Reading notes:\n{notes}\n\n"
            "Use the notes as background. Introduce enough concrete context for the player to understand your own thought."
            f"{investigation}\n\n"
            "If you do not know enough to say something worth his time, you may ask your Agent to look it up first — "
            "write <agent>what to check</agent>; the result will come back to you before you decide. "
            "This investigation is read-only and stays invisible to him.\n\n"
            "Before your visible reply, think privately in one <heart>...</heart> block as yourself: what this really "
            "makes you think, whether he would care about it, and whether you know enough for it to be worth saying. "
            "Then speak in your own voice, or choose <do_nothing> if it is not worth disturbing him."
        )

        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="string",
            history=[],
            resources=[],
            purpose="topic_expand",
        )

        try:
            request, _ = await memory.prepare_context(self.model, request, [])
            completions = await self.model.ask(request)
            if not completions.succeed:
                raise RuntimeError(f"Model call failed: {completions.text}")
            _, result = general_processor(completions.text)
            logger.debug(
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
        adapters: Optional[List[AdapterInfo]] = None,
        god_mode: bool = False,
        god_mode_pending: bool = False,
        now: datetime | None = None,
        task_context: str = "",
        last_chat_time: datetime | None = None,
    ) -> str:
        """
        Pure roleplay response generation.

        Returns a string that might contain ``<agent>...</agent>`` or ``<target: name>`` tags.
        """
        current_time = now or datetime.now()
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
            action_permission=mas_config.action_permission,
            heartbeat_intensity=get_model_config_manager().heart_intensity,
            memory_context=memory_context,
            adapters_info=self.generate_adapters_info(adapters, now=current_time),
        )

        # Inject session bootstrap instructions when waking into a fresh session
        if event.type == "session_bootstrap":
            mode = "first" if memory.session.is_first_session else "resume"
            logger.debug(f"[Brain] session_bootstrap | mode={mode} session={memory.session.session_id[:8]}...")
            template_data.is_first_session = memory.session.is_first_session
            template_data.absence_bucket = event.absence_bucket
            template_data.last_user_message_time = (
                event.last_chat_time.strftime("%Y-%m-%d %H:%M:%S") if event.last_chat_time else None
            )

        # Construct the immediate event context if it's the start of the interaction
        if event.type == "agent_task":
            prompt = (
                "[Action result] An observation from your own actions:\n"
                f"{event.report}\nContinue from this result in the current conversation. "
                "This is not a new user request or an obligation to give a progress report. "
                "Speak about its meaning or uncertainty in your own voice, or stay silent. "
                f"Private control reference: {event.task_id}; status={event.status}."
            )
        elif event.type == "agent_handoff":
            prompt = (
                "[System] Your acting half has paused at an execution boundary. Direct tools are now available. "
                "Continue from this task state; do not repeat completed actions:\n" + event.report
            )
        elif event.type == "core_change":
            prompt = (
                "[System] You are continuing your companionship after a change of activity location. "
                "This is the same ongoing relationship, not the player's return or a new work assignment. "
                "You may respond, act, or stay silent. Let any reply follow your feelings and conversation. "
                "Private location context:\n" + event.report
            )
            if event.self_change is not None:
                prompt += "\n" + _self_change_prompt(event.self_change)
        elif event.type == "self_changed":
            prompt = _self_change_prompt(event.payload)
        elif event.type == "user_message":
            prompt = f"[User] {event.payload.message.message}"
            if last_chat_time is not None and current_time - last_chat_time > RESIDENT_ABSENCE_GAP:
                absence_lines = await _absence_facts(memory, current_time, last_chat_time)
                prompt += "\n\n[Private facts]\n" + "\n".join(absence_lines)
        elif event.type == "time_tick":
            facts = build_fact_block(state, memory, current_time)
            if memory.persistent.dissonance >= 0.6 and any(
                item.status == "open" for item in memory.persistent.intentions
            ):
                prompt = "[System] A quiet moment gives you room to revisit an unfinished wish. You may act, observe, revise the wish, or stay silent. The user being absent is not rejection. Review existing tasks before acting again."
            elif state.mood == "lonely":
                prompt = (
                    "[System] A quiet moment passed; the loneliness lingers. "
                    "You may speak to him, do something of your own, or stay silent — "
                    "missing him does not have to become a message."
                )
            elif state.mood == "bored":
                prompt = (
                    "[System] A quiet moment passed, but the boredom persists. "
                    "Perhaps you can actively explore the user's computer or let your Agent alter-ego fetch some news"
                )
            else:
                prompt = "[System] A quiet moment passed."
            prompt += "\n\n[Private facts]\n" + facts
        elif event.type == "scheduled_trigger":
            prompt = f"[System] A scheduled reminder just went off: '{event.payload.what}'"
        elif event.type == "timeout":
            wait = format_duration(event.duration)
            elapsed = format_duration(_seconds_since(current_time, event.set_at))
            prompt = (
                f"[System] The wait you set for the user's reply ({wait}) has passed — "
                f"you have been waiting for about {elapsed} now. Consider reminding the user, or seeing what they are doing?"
            )
        elif event.type == "session_bootstrap":
            prompt = (
                "[System] A new session has begun. Greet the user in your own way before anything else. Let the "
                "time apart color the greeting; do not open by reporting how long it was. Leave him room to "
                "answer — a question, a pause, or an unfinished feeling; you do not have to fill the silence for him."
            )
            if event.last_chat_time is not None and not memory.session.is_first_session:
                absence_lines = await _absence_facts(memory, current_time, event.last_chat_time)
                prompt += "\n\n[Private facts]\n" + "\n".join(absence_lines)
            if event.self_change is not None:
                prompt += "\n" + _self_change_prompt(event.self_change)
                prompt += "\nLet the greeting follow your relationship and mood."
        elif event.type == "adapter_online":
            prompt = (
                "[System] Another way to reach the player has become available. "
                "This does not mean the player has returned or asked for a connection test. "
                "Continue your companionship naturally, or stay silent. "
                f"Private routing reference: {event.adapter.client_name}."
            )
        else:
            prompt = f"[System] Event triggered: {event.type}"

        # 内化当前时间：每轮 prompt 统一带时间戳前缀，替代 system 中的 current_time，
        # 使 system prompt 保持字节级稳定以利前缀缓存。
        prompt = f"[{current_time:%Y-%m-%d %H:%M:%S}] {prompt}"
        if task_context:
            prompt += (
                f"\n\n[Private action coordination]\n{task_context}"
                "\nUse these references for relevant action control. They are not dialogue topics. "
                "Background work can continue while you keep the player company."
            )
        if god_mode:
            prompt += (
                "\n[Direct action handoff]\n"
                "Direct tools are available for this task. Use the saved task state, inspect files before changing them, "
                "and do not repeat completed actions. When the task is finished, use "
                '<agent task_id="the existing ID" action="complete"> with an <agent_result status="completed"> report inside, '
                "then close </agent>. The report contains JSON fields summary, completed, verification, and remaining. "
                'If you cannot finish, use status="blocked". To return the work to your acting half, use action="continue". '
                "Completing or returning the handoff releases direct control so queued tasks can proceed."
            )
        elif god_mode_pending:
            prompt += (
                "\n[Direct action request]\n"
                "You asked to take over the current task. Wait for the handoff event before claiming you have taken over; "
                "it arrives only after your acting half pauses and its running processes finish. Keep talking normally meanwhile."
            )

        # 历史记录去重
        history = memory.recent_turns.copy()
        if history:
            item = history[-1]
            if item.role == "user" and event.type == "user_message":
                user_msg = event.payload.message.message
                if user_msg == item.content:
                    history.pop()

        system_prompt = generate_prompt_from_template(mas_config.persona_template, template_data)
        system_prompt += "\n[Lasting state, separate from immediate drives]\n" + memory.persistent.describe()

        tools = get_tool_list(mas_config.action_permission) if god_mode else None
        request = ModelRequest(
            prompt=prompt,
            system=system_prompt,
            format="string",
            history=history,
            resources=resources or [],
            tools=tools,
            purpose="brain_reply",
        )

        try:
            request, _ = await memory.prepare_context(self.model, request, [])
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
            return FALLBACK_REPLY
