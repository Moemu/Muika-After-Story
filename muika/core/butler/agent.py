"""
Butler Agent — maps freeform natural-language commands (from Muika's <Butler:> tags) to
tool-augmented LLM calls, then returns the final text response to the Brain.

The butler operates by passing all registered tools to the LLM via ModelRequest.tools.
The LLM provider (DashScope / OpenAI / etc.) handles tool call dispatch internally:
  1. LLM decides which tool(s) to call → provider executes via function_call_handler.
  2. Provider feeds tool results back to LLM → LLM produces final text.
  3. Butler returns that text as the butler report.

This means execute_command() always returns a human-readable string, and Muika (the Brain)
never receives raw tool data — just a polished butler report.

External plugins register tools via @on_function_call decorator.
"""

from __future__ import annotations

import json

from muika.config import get_model_config, mas_config

# Import tool modules so @on_function_call registrations happen at import time.
from muika.core.actions import tools as _tools  # noqa: F401
from muika.core.butler._prompts import (
    PREFERENCE_MATCH_PROMPT,
    SESSION_SUMMARY_PROMPT,
    TOOL_SELECTION_PROMPT,
)
from muika.core.executor import Executor
from muika.core.memory import MemoryRecord, SessionTurn
from muika.core.state import MuikaState
from muika.llm import ModelRequest, load_model
from muika.models import Resource
from muika.plugin.func_call import get_function_list
from muika.plugin.func_call._context import (
    clear_butler_context,
    get_resources,
    set_butler_context,
)
from muika.plugin.skills import get_skill_manager
from muika.utils.logger import logger

# Re-bind with private aliases to avoid breaking the rest of this module.
_TOOL_SELECTION_PROMPT = TOOL_SELECTION_PROMPT
_PREFERENCE_MATCH_PROMPT = PREFERENCE_MATCH_PROMPT
_SESSION_SUMMARY_PROMPT = SESSION_SUMMARY_PROMPT


# ---------------------------------------------------------------------------
# ButlerAgent
# ---------------------------------------------------------------------------


class ButlerAgent:
    """
    Receives a natural-language command, passes it to the LLM with all registered tools,
    and returns the LLM's final text response as a butler report.

    Tool call dispatch is handled entirely by the LLM provider — the Butler does not
    need its own execution loop.
    """

    def __init__(self) -> None:
        butler_cfg = get_model_config(mas_config.butler_model) if mas_config.butler_model else None
        self.model = load_model(butler_cfg)
        self.tools = get_function_list()

        logger.debug(f"Loaded {len(self.tools)} tools.")

        # 技能管理器：启动时扫描技能目录并启动热重载监听
        self._skill_manager = get_skill_manager()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_relevant_preferences(
        self,
        user_input: str,
        preferences: list[MemoryRecord],
    ) -> list[MemoryRecord]:
        """
        预处理层：对用户输入做一次轻量 LLM 推断，
        返回 PreferenceProfile 中与当前输入语义相关的条目。

        若 preferences 为空则直接短路返回，不消耗任何推理资源。
        """
        if not preferences:
            logger.debug("[Butler/Preprocess] No PREFERENCE records available, skipping LLM match.")
            return []

        logger.debug(
            f"[Butler/Preprocess] Running preference match | "
            f"input={user_input[:60]!r} candidates={len(preferences)}"
        )
        records_text = "\n".join(
            f"- key={r.key!r}, category={r.category.value}, value={r.value!r}" for r in preferences
        )
        prompt = f"User message: {user_input!r}\n\nPreference records:\n{records_text}"

        request = ModelRequest(
            prompt=prompt,
            system=_PREFERENCE_MATCH_PROMPT,
            format="json",
        )

        try:
            completion = await self.model.ask(request=request, stream=False)
            data = json.loads(completion.text)
            relevant_keys: set[str] = set(data.get("relevant_keys", []))
            matched = [r for r in preferences if r.key in relevant_keys]
            logger.debug(
                f"[Butler/Preprocess] Match result | "
                f"relevant_keys={sorted(relevant_keys)} "
                f"matched={len(matched)}/{len(preferences)}"
            )
            if matched:
                logger.info(
                    f"[Butler/Preprocess] Injecting {len(matched)} preference(s): " f"{[r.key for r in matched]}"
                )
            return matched
        except Exception as e:
            logger.warning(f"[Butler/Preprocess] Preference match failed: {e}")
            return []

    async def summarize_session(self, turns: list[SessionTurn]) -> str:
        """
        对本次 Session 的对话记录生成一段简洁的文字摘要，供写入 ARCHIVE 层。
        由 loop.py 在 session_end 事件时调用，不走 Butler 内循环。
        """
        if not turns:
            logger.debug("[Butler/Summary] No turns to summarize — returning empty string.")
            return ""

        transcript = "\n".join(f"[{t.role.upper()}] {t.content}" for t in turns)
        logger.info(f"[Butler/Summary] Summarizing {len(turns)} turns...")

        request = ModelRequest(
            prompt=f"Session transcript:\n\n{transcript}",
            system=_SESSION_SUMMARY_PROMPT,
        )
        try:
            completion = await self.model.ask(request=request, stream=False)
            summary = completion.text.strip()
            logger.info(
                f"[Butler/Summary] Done — {len(summary)} chars: {summary[:120]!r}{'...' if len(summary) > 120 else ''}"
            )
            return summary
        except Exception as e:
            logger.error(f"[Butler/Summary] Summarization LLM failed: {e}")
            return f"[Summary failed: {e}]"

    async def execute_command(
        self,
        command: str,
        state: MuikaState,
        executor: Executor,
    ) -> tuple[str, list[Resource]]:
        """
        Execute *command* by passing it to the LLM with all registered tools.

        The LLM provider handles tool call dispatch internally. Returns (report, resources).
        """
        logger.info(f"[Butler] Executing command: {command!r}")

        # 注入 Butler 上下文，让工具函数能访问 state 和 executor
        set_butler_context(state, executor)
        try:
            # 组装系统提示，注入可用技能列表
            system = _TOOL_SELECTION_PROMPT
            skills_section = self._skill_manager.render_prompt_section()
            if skills_section:
                system += f"\n\n{skills_section}"

            request = ModelRequest(
                prompt=f"Command: {command}",
                system=system,
                tools=self.tools,
            )

            try:
                completion = await self.model.ask(request=request, stream=False)
                report = completion.text.strip()
            except Exception as e:
                logger.error(f"[Butler] LLM error: {e}")
                return (f"I encountered an error while executing the command: {e}", [])

            # 收集工具执行过程中产生的资源（图片等）
            resources = get_resources()

            if report:
                logger.info(f"[Butler] Report ready ({len(report)} chars): {report[:120]!r}")
            else:
                logger.debug("[Butler] Empty report (silent operation).")

            return (report, resources)
        finally:
            clear_butler_context()
