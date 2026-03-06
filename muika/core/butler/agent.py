"""
Butler Agent — maps freeform natural-language commands (from Muika's <Butler:> tags) to typed
Action objects using an LLM, then dispatches to their handle() methods.

The butler operates as a mini-agent with its own inner loop:
  1. Select the most appropriate tool via LLM (structured JSON output).
  2. Execute the tool → get ActionOutput.
  3. Analyse the result via a second LLM call: either synthesise a concise natural-language
     report ("done") or request a retry with a different approach.
  4. Repeat up to MAX_BUTLER_LOOPS times; return a plain str to the caller.

This means execute_command() always returns a human-readable string, and Muika (the Brain)
never receives raw tool data — just a polished butler report.

External plugins simply subclass BaseAction (or BaseTool/BaseIntent) and implement handle().
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Union

from nonebot import logger
from pydantic import BaseModel, Field, TypeAdapter

from muika.config import get_model_config, mas_config

# Import modules for built-in action side-effects so subclasses are loaded.
from muika.core.actions import BaseAction
from muika.core.actions import intents as _intents  # noqa: F401
from muika.core.actions import tools as _tools  # noqa: F401
from muika.core.butler._prompts import (
    ANALYSIS_PROMPT,
    PREFERENCE_MATCH_PROMPT,
    SESSION_SUMMARY_PROMPT,
    TOOL_SELECTION_PROMPT,
)
from muika.core.constants import MAX_BUTLER_LOOPS
from muika.core.executor import Executor
from muika.core.memory import MemoryRecord, SessionTurn
from muika.core.state import MuikaState
from muika.llm import ModelRequest, load_model

# ---------------------------------------------------------------------------
# Prompts  (→ see muika/core/butler/_prompts.py)
# ---------------------------------------------------------------------------

# Re-bind with private aliases to avoid breaking the rest of this module.
_TOOL_SELECTION_PROMPT = TOOL_SELECTION_PROMPT
_PREFERENCE_MATCH_PROMPT = PREFERENCE_MATCH_PROMPT
_SESSION_SUMMARY_PROMPT = SESSION_SUMMARY_PROMPT
_ANALYSIS_PROMPT = ANALYSIS_PROMPT

# ---------------------------------------------------------------------------
# Analysis response schema
# ---------------------------------------------------------------------------


class _AnalysisDone(BaseModel):
    status: Literal["done"]
    report: str


class _AnalysisRetry(BaseModel):
    status: Literal["retry"]
    reason: str


_AnalysisResult = Annotated[
    Union[_AnalysisDone, _AnalysisRetry],
    Field(discriminator="status"),
]
_analysis_adapter: TypeAdapter[_AnalysisResult] = TypeAdapter(_AnalysisResult)


# ---------------------------------------------------------------------------
# ButlerAgent
# ---------------------------------------------------------------------------


class ButlerAgent:
    """
    Receives a natural-language command, iteratively selects and executes tools,
    and returns a plain-string butler report to the Brain.

    The Union type for tool selection is built dynamically from __subclasses__(),
    so external plugins that subclass BaseAction are auto-discovered.
    """

    @staticmethod
    def _leaf_action_classes(base_cls: type[BaseAction]) -> list[type[BaseAction]]:
        leaves: list[type[BaseAction]] = []

        def walk(cls: type[BaseAction]) -> None:
            subs = cls.__subclasses__()
            if not subs:
                if not cls.__name__.startswith("Base") and getattr(cls, "is_enabled", lambda: True)():
                    leaves.append(cls)
                return
            for sub in subs:
                walk(sub)

        walk(base_cls)
        return leaves

    def __init__(self) -> None:
        butler_cfg = get_model_config(mas_config.butler_model) if mas_config.butler_model else None
        self.model = load_model(butler_cfg)
        action_classes = self._leaf_action_classes(BaseAction)

        if not action_classes:
            raise RuntimeError("No Action subclasses found. Did you forget to import them?")

        logger.debug(
            f"[ButlerAgent] Discovered {len(action_classes)} action(s): " f"{[c.__name__ for c in action_classes]}"
        )

        # Build a Pydantic discriminated union over all concrete action classes
        ActionUnion = Annotated[  # type: ignore[valid-type]
            Union[tuple(action_classes)],  # type: ignore[arg-type]
            Field(discriminator="name"),
        ]
        self._action_adapter: TypeAdapter[BaseAction] = TypeAdapter(ActionUnion)

        # JSON schema embedded in the tool-selection prompt
        self._schema_json = json.dumps(self._action_adapter.json_schema(), ensure_ascii=False, indent=2)

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
    ) -> str:
        """
        Execute *command* using a mini inner loop:
          • select tool → execute → analyse result → retry or return.
        Always returns a plain-string butler report (never raw tool data).
        """
        logger.info(f"[Butler] Executing command: {command!r}")

        # Maintains the execution context for complex/multi-step requests
        execution_history: list[dict[str, str]] = []

        for attempt in range(1, MAX_BUTLER_LOOPS + 1):
            logger.debug(f"[Butler] Attempt {attempt}/{MAX_BUTLER_LOOPS}")

            # ── Step 1: LLM selects tool ──────────────────────────────────
            prompt_payload = f"Command: {command}"
            if execution_history:
                prompt_payload += "\n\n### Execution History ###\n"
                for i, turn in enumerate(execution_history, 1):
                    prompt_payload += (
                        f"--- Round {i} ---\n"
                        f"Tool: {turn['tool']}\n"
                        f"Arguments: {turn['args']}\n"
                        f"Output Preview: {turn['output'][:500]}...\n"
                        f"Analysis/Next Steps: {turn.get('analysis', 'None')}\n"
                    )

            selection_request = ModelRequest(
                prompt=prompt_payload,
                system=f"{_TOOL_SELECTION_PROMPT}\n\nAvailable actions (JSON schema):\n{self._schema_json}",
                format="json",
                json_schema=self._action_adapter,
            )

            try:
                sel_completion = await self.model.ask(request=selection_request, stream=False)
                raw_action = sel_completion.text
                logger.debug(f"[Butler] Tool selection response: {raw_action!r}")
            except Exception as e:
                logger.error(f"[Butler] Tool selection LLM error: {e}")
                return f"I encountered an error while choosing a tool: {e}"

            try:
                action = self._action_adapter.validate_json(raw_action)
            except Exception as e:
                logger.error(f"[Butler] Failed to parse action JSON: {e}\nRaw: {raw_action!r}")
                return f"I failed to understand how to handle that command: {e}"

            logger.info(f"[Butler] Dispatching: {type(action).__name__}")

            # ── Step 2: Execute tool ──────────────────────────────────────
            try:
                output = await action.handle(state, executor)
                tool_result_text = output.content
            except Exception as e:
                logger.exception(f"[Butler] {type(action).__name__} raised: {e}")
                tool_result_text = f"[Tool error] {e}"
                output = None  # mark as non-silent on error

            logger.debug(f"[Butler] Raw tool output ({len(tool_result_text)} chars): {tool_result_text[:300]!r}")

            # 副作用操作（silent=True）无需 Analysis 和回报，直接返回空字符串
            if output is not None and output.silent:
                logger.debug(
                    f"[Butler] Silent operation ({type(action).__name__}) — "
                    f"skipping Analysis LLM, no report injected."
                )
                return ""

            # Record this turn in the history
            turn_record = {
                "tool": type(action).__name__,
                "args": action.model_dump_json(exclude_none=True),
                "output": tool_result_text,
            }
            execution_history.append(turn_record)

            # ── Step 3: Analyse result ────────────────────────────────────
            # The analysis step gets the whole history so it understands the full context
            analysis_prompt = (
                f"{prompt_payload}\n\n"
                f"--- Round {attempt} (Current) ---\n"
                f"Tool used: {turn_record['tool']}\n"
                f"Arguments: {turn_record['args']}\n"
                f"Tool result:\n{tool_result_text}\n\n"
                f"Determine if the overall command '{command}' is complete, or if a retry/next step is needed."
            )

            analysis_request = ModelRequest(
                prompt=analysis_prompt,
                system=_ANALYSIS_PROMPT,
                format="json",
                json_schema=_analysis_adapter,
            )

            try:
                ana_completion = await self.model.ask(request=analysis_request, stream=False)
                analysis = _analysis_adapter.validate_json(ana_completion.text)
            except Exception as e:
                # If analysis itself fails, treat the raw output as the report
                logger.warning(f"[Butler] Analysis LLM failed ({e}), falling back to raw output")
                return tool_result_text

            if analysis.status == "done":
                logger.info(f"[Butler] Report ready after {attempt} attempt(s).")
                return analysis.report  # type: ignore[union-attr]

            # status == "retry"
            reason: str = analysis.reason  # type: ignore[union-attr]
            logger.info(f"[Butler] Requesting next step / retry: {reason}")
            turn_record["analysis"] = reason

        return "I was unable to complete the task after several steps. Please try a different approach."
