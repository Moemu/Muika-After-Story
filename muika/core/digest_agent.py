import hashlib
import random
import re
from dataclasses import dataclass
from typing import Optional

from nonebot import logger
from nonebot_plugin_orm import get_scoped_session
from pydantic import BaseModel, Field

from muika.config import get_model_config, mas_config
from muika.core.actions.tools.rss._parser import (
    ParsedResult,
    extract_web_content,
    fetch_web_content,
    parse_rss_feed,
)
from muika.core.actions.tools.rss._source import RSS_SOURCES
from muika.core.constants import DIGEST_MIN_SCORE
from muika.core.topic_manager import EventTopic, TopicManager, TopicSource
from muika.database.crud import TopicHistoryCRUD
from muika.llm import ModelRequest, load_model

_MAX_CANDIDATES_PER_SOURCE = 12
_MAX_EVALUATIONS_PER_SOURCE = 8

_TOPIC_FILTER_SYSTEM_PROMPT = (
    "You are a strict topic gatekeeper for Muika (Monika-style persona). "
    "Judge whether a news item is worth becoming Muika's proactive conversation topic. "
    "Muika strongly prefers: world affairs and peace/human conflict, "
    "philosophy and human condition, "
    "literature and art, and society-level issues that invite reflection. "
    "Muika generally dislikes purely technical optimization updates "
    "unless they carry strong human/social implications. "
    "Return JSON only."
)


class TopicFitAssessment(BaseModel):
    score: int = Field(ge=0, le=100)
    keep: bool
    reason: str
    primary_theme: str = Field(default="other")
    summary: str = Field(default="")


@dataclass
class ScoredCandidate:
    score: int
    entry: ParsedResult
    topic_id: str
    summary: str
    primary_theme: str


class DigestAgent:
    """
    后台摘要生成代理。
    定期抓取 RSS，将其转化为 Muika 的个人阅读笔记，然后作为 EventTopic 注入 TopicManager 的队列中。
    """

    def __init__(self, topic_manager: TopicManager):
        self.topic_manager = topic_manager
        butler_cfg = get_model_config(mas_config.butler_model) if mas_config.butler_model else None
        self.model = load_model(butler_cfg)
        if mas_config.butler_model:
            logger.info(f"[DigestAgent] Using Butler model config: {mas_config.butler_model}")
        else:
            logger.warning(
                "[DigestAgent] `butler_model` is not configured; DigestAgent is using default model config. "
                "Set `butler_model` in plugin config to reduce digest cost."
            )

    @staticmethod
    def _normalize_text(text: str) -> str:
        cleaned = re.sub(r"\s+", " ", text or "").strip().lower()
        return cleaned

    async def _assess_entry_for_muika(
        self,
        source_name: str,
        title: str,
        content: str,
    ) -> Optional[TopicFitAssessment]:
        """使用一次 LLM 调用同时完成：Muika 话题适配评估 + 摘要生成。"""
        text = self._normalize_text(f"{title} {content}")
        if len(text) < 40:
            return None

        prompt = (
            "Evaluate this article candidate from Muika's perspective.\n"
            "Return JSON with fields: score(0-100), keep(boolean), reason(short), primary_theme, summary.\n"
            "Scoring guide:\n"
            "- 80-100: Strongly aligned (world affairs/philosophy/literature/"
            "human condition with high discussability).\n"
            "- 60-79: Moderately aligned (can still trigger meaningful reflection).\n"
            "- 0-59: Weakly aligned (too technical, too trivial, or no emotional/reflection potential).\n\n"
            "Summary requirements:\n"
            "- If keep=true: summary must be a concise Chinese paragraph covering what happened, "
            "who is involved, and why it matters.\n"
            "- If keep=false: summary can be an empty string.\n\n"
            f"Source: {source_name}\n"
            f"Title: {title}\n"
            f"Content: {content}\n"
        )

        request = ModelRequest(
            prompt=prompt,
            system=_TOPIC_FILTER_SYSTEM_PROMPT,
            format="json",
            json_schema=TopicFitAssessment,
            history=[],
            resources=[],
        )

        try:
            resp = await self.model.ask(request)
            if not resp.succeed:
                return None
            return TopicFitAssessment.model_validate_json(resp.text)
        except Exception as e:
            logger.warning(f"[DigestAgent] Topic fit evaluation failed: {e}")
            return None

    async def fetch_and_digest(self) -> None:
        """尝试抓取并生成一篇未读新闻的摘要。"""
        sources = [source for source in RSS_SOURCES.values() if source.digest]
        db_session = get_scoped_session()

        for source in sources:
            try:
                logger.debug(f"[DigestAgent] Fetching RSS: {source.name}")
                rss_content = await fetch_web_content(source.url)
                entries = parse_rss_feed(rss_content)

                entries = random.sample(entries, min(len(entries), _MAX_CANDIDATES_PER_SOURCE))

                scored_candidates: list[ScoredCandidate] = []
                for idx, entry in enumerate(entries):
                    if idx >= _MAX_EVALUATIONS_PER_SOURCE:
                        break

                    url_hash = hashlib.md5(entry.link.encode()).hexdigest()[:12]
                    topic_id = f"event_rss_{source.id}_{url_hash}"

                    # 检查是否已处理过
                    history = await TopicHistoryCRUD.get_by_topic_id(db_session, topic_id)
                    if history:
                        continue

                    content = await extract_web_content(entry.link)
                    if not content:
                        continue

                    assessment = await self._assess_entry_for_muika(
                        source_name=source.name,
                        title=entry.title,
                        content=content,
                    )
                    if assessment is None:
                        continue

                    if (not assessment.keep) or assessment.score < DIGEST_MIN_SCORE:
                        logger.debug(
                            f"[DigestAgent] Skip low-fit entry ({assessment.score}) from {source.name}: "
                            f"{entry.title} reason={assessment.reason}"
                        )
                        continue

                    if not assessment.summary.strip():
                        logger.debug(f"[DigestAgent] Skip kept entry without summary: {entry.title}")
                        continue

                    scored_candidates.append(
                        ScoredCandidate(
                            score=assessment.score,
                            entry=entry,
                            topic_id=topic_id,
                            summary=assessment.summary.strip(),
                            primary_theme=assessment.primary_theme,
                        )
                    )

                if not scored_candidates:
                    logger.debug(f"[DigestAgent] No candidate passed score gate in source: {source.name}")
                    continue

                scored_candidates.sort(key=lambda item: item.score, reverse=True)
                chosen = scored_candidates[0]

                logger.info(
                    f"[DigestAgent] Digesting selected entry score={chosen.score} source={source.name}: "
                    f"{chosen.entry.title}"
                )

                # 压入话题队列
                topic = EventTopic(
                    id=chosen.topic_id,
                    source=TopicSource.EVENT,
                    category=source.id,
                    title=chosen.entry.title,
                    content=chosen.summary,
                    date=chosen.entry.published,
                    cooldown_days=7,
                    tags=["news", source.id, chosen.primary_theme],
                )
                self.topic_manager.enqueue_event(topic)

                # 占位符写入记录，避免下次重复抓取
                await TopicHistoryCRUD.record(db_session, topic_id=chosen.topic_id, user_engaged=False)
                await db_session.commit()

                logger.success(f"[DigestAgent] Successfully digested and enqueued: {chosen.topic_id}")
                return  # 每次只处理 1 篇新内容

            except Exception as e:
                logger.error(f"[DigestAgent] Error fetching source {source.name}: {e}")

        logger.debug("[DigestAgent] No new unread entries found in checked sources.")
