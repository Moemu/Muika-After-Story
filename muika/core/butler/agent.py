"""执行 Muika 的行动意图，并提供记忆检索、分类和摘要。"""

from __future__ import annotations

import json
import re

from muika.config import get_model_config, mas_config

# 导入工具模块以完成注册。
from muika.core.actions import tools as _tools  # noqa: F401
from muika.core.butler._prompts import (
    MEMORY_CLASSIFICATION_PROMPT,
    PREFERENCE_MATCH_PROMPT,
    SESSION_SUMMARY_PROMPT,
)
from muika.core.executor import Executor
from muika.core.memory import MemoryCategory, MemoryLayer, MemoryRecord, SessionTurn
from muika.core.state import MuikaState
from muika.llm import ModelRequest, load_model
from muika.llm.utils.thought_processor import general_processor
from muika.models import Resource
from muika.plugin.func_call import get_tool_list
from muika.plugin.func_call.context import tool_context
from muika.plugin.skills import get_skill_manager
from muika.template.loader import generate_prompt_from_template
from muika.utils.logger import logger

AGENT_RESULT_PATTERN = re.compile(
    r"<agent_result\s+status=[\"'](completed|blocked)[\"']>(.*?)</agent_result>",
    re.DOTALL,
)


def _parse_agent_report(text: str) -> str:
    match = AGENT_RESULT_PATTERN.fullmatch(text.strip())
    if match:
        report = match.group(2).strip()
        return report if match.group(1) == "completed" else f"Agent blocked: {report}"
    last_output = text.strip() or "(empty response)"
    return f"Agent stopped before reporting completion. Last output: {last_output}"


class ButlerAgent:
    """
    Receives a natural-language command, passes it to the LLM with all registered tools,
    and returns the LLM's final text response as a butler report.

    Tool call dispatch is handled entirely by the LLM provider — the Butler does not
    need its own execution loop.
    """

    def __init__(self) -> None:
        butler_cfg = get_model_config(mas_config.butler_model)
        summarize_model_cfg = get_model_config(mas_config.session_summarize_model or mas_config.butler_model)
        self.model = load_model(butler_cfg)
        self.summarize_model = load_model(summarize_model_cfg)

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
            system=PREFERENCE_MATCH_PROMPT,
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
            system=SESSION_SUMMARY_PROMPT,
        )
        try:
            completions = await self.summarize_model.ask(request=request, stream=False)
            if not completions.succeed:
                raise RuntimeError(f"Session summary failed: {completions.text}")
            _, summary = general_processor(completions.text)
            summary = summary.strip()
            if not summary:
                raise ValueError("Session summary is empty")
            logger.info(
                f"[Butler/Summary] Done — {len(summary)} chars: {summary[:120]!r}{'...' if len(summary) > 120 else ''}"
            )
            return summary
        except Exception as e:
            logger.error(f"[Butler/Summary] Summarization LLM failed: {e}")
            raise

    async def classify_and_store_memory(
        self,
        content: str,
        state: MuikaState,
        max_retry: int = 3,
    ):
        """
        对从 <memory> 标签提取的原始记忆内容进行分类并存入记忆系统。
        """
        if not content.strip():
            logger.debug("[Butler/Memory] Empty content -- skipping.")
            return

        logger.info(f"[Butler/Memory] Classifying: {content[:80]!r}...")

        request = ModelRequest(
            prompt=f"Raw memory note:\n\n{content}",
            system=MEMORY_CLASSIFICATION_PROMPT,
            format="json",
        )

        try:
            completion = await self.model.ask(request=request, stream=False)
            data = json.loads(completion.text)
            if not data.get("should_store", True):
                logger.info(f"[Butler/Memory] Classifier declined storage: {data.get('reason', 'no reason')}")
                return
            layer = MemoryLayer(data["layer"])
            category = MemoryCategory(data["category"])
            key = data["key"]
            value = str(data["value"]).strip()
        except Exception as e:
            logger.warning(f"[Butler/Memory] Classification LLM failed: {e}, retrying...")
            if max_retry > 0:
                return await self.classify_and_store_memory(content, state, max_retry - 1)
            else:
                logger.error("[Butler/Memory] Classification LLM failed, give up.")
                return

        if state.memory is None:
            logger.warning("[Butler/Memory] MemoryManager not on state -- cannot store.")
            return

        if not value:
            logger.warning("[Butler/Memory] Classifier returned an empty value -- skipping.")
            return

        await state.memory.upsert_memory(
            layer=layer,
            category=category,
            key=key,
            value=value,
        )
        logger.info(f"[Butler/Memory] Stored: [{layer.value}/{category.value}] " f"{key} = {value[:60]!r}...")

    async def execute_command(
        self,
        command: str,
        state: MuikaState,
        executor: Executor,
    ) -> tuple[str, list[Resource]]:
        """调用模型执行行动意图，返回执行报告和工具资源。"""
        logger.info(f"[Butler] Executing command: {command!r}")

        with tool_context(state, executor) as context:
            # 组装系统提示（Muika 的行动半身模板），注入可用技能列表
            system = generate_prompt_from_template(mas_config.agent_template)

            skills_section = self._skill_manager.render_prompt_section()
            if skills_section:
                system += f"\n\n{skills_section}"

            request = ModelRequest(
                prompt=f"Command: {command}",
                system=system,
                tools=get_tool_list(),
            )

            try:
                completion = await self.model.ask(request=request, stream=False)
                report = _parse_agent_report(completion.text)
            except Exception as e:
                logger.error(f"[Butler] LLM error: {e}")
                return (f"I encountered an error while executing the command: {e}", [])

            # 收集工具执行过程中产生的资源（图片等）
            resources = context.resources

            if report:
                logger.info(f"[Butler] Report ready ({len(report)} chars): {report[:120]!r}")
            else:
                logger.debug("[Butler] Empty report (silent operation).")

            return (report, resources)
