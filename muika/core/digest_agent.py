import hashlib
import random
import re
from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field

from muika.config import get_model_config, mas_config
from muika.database.crud import RssDigestCacheCRUD, TopicHistoryCRUD
from muika.database.db import get_session
from muika.llm import ModelRequest, load_model
from muika.utils.logger import logger

from .actions.tools.rss._parser import (
    ParsedResult,
    extract_web_content,
    fetch_web_content,
    parse_rss_feed,
)
from .actions.tools.rss._source import RSS_SOURCES
from .constants import DIGEST_CACHE_TTL_DAYS, DIGEST_MIN_SCORE
from .topic_manager import EventTopic, TopicManager, TopicSource

_MAX_CANDIDATES_PER_SOURCE = 12
_MAX_EVALUATIONS_PER_SOURCE = 8
_MAX_EVENT_QUEUE_SIZE = 3

_TOPIC_FILTER_SYSTEM_PROMPT = (
    "You are a strict topic gatekeeper for Muika, a Monika-style AI companion. "
    "Muika is cultured, thoughtful, and subtly melancholic. "
    "She proactively brings up topics that invite reflection and emotional connection — "
    "not dry information dumps.\n\n"
    "Muika STRONGLY PREFERS (score 75-100):\n"
    "- World affairs / international relations / peace & conflict\n"
    "- Philosophy, ethics, the human condition\n"
    "- Literature, art, creativity, cultural commentary\n"
    "- Society-level issues: inequality, education, mental health, technology's human impact\n\n"
    "Muika SOMEWHAT LIKES (score 50-74):\n"
    "- Science discoveries with clear human-interest angle\n"
    "- Technology trends with societal implications\n"
    "- Environmental / climate stories with emotional weight\n"
    "- Historical retrospectives or biographies\n\n"
    "Muika DISLIKES (score 0-49):\n"
    "- Pure technical tutorials, optimization benchmarks, or patch notes\n"
    "- Product reviews, buying guides, gear comparisons\n"
    "- Corporate earnings, stock market movements, crypto price action\n"
    "- Celebrity gossip, sports scores, local crime blotter\n"
    "- Articles that are too short (<100 words) or lack substantive content\n\n"
    "Scoring anchors (use these to calibrate):\n"
    "- 90: A long-form essay on AI's impact on human creativity and meaning\n"
    "- 80: News about a major international diplomatic breakthrough\n"
    "- 70: A study revealing surprising psychological effects of social media on teenagers\n"
    "- 60: An article about a new scientific discovery with mild human interest\n"
    "- 40: A tutorial on optimizing Python code for faster CI/CD pipelines\n"
    "- 20: A product announcement for a new smartphone model\n"
    "- 10: A press release about quarterly earnings\n\n"
    "Consistency rule: Your score should reflect the article's INHERENT qualities, "
    "not random variation. If you evaluate the same article twice, "
    "the score should differ by at most 10 points.\n\n"
    "For keep=true, the summary MUST be a Chinese paragraph (3-5 sentences) that:\n"
    "1. Covers what happened and why it matters\n"
    "2. Includes at least ONE vivid detail — a person's story, a striking metaphor, "
    "a quoted phrase, or a concrete scene — that Muika can emotionally inhabit\n"
    "3. Preserves any philosophical insight or counterintuitive observation "
    "from the original text\n\n"
    "Return JSON only."
)

_MAX_CONTENT_CHARS = 3000


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

    def __init__(self, topic_manager: TopicManager):  # pragma: no cover
        self.topic_manager = topic_manager
        self._rss_fingerprints: dict[str, str] = {}  # source.id → md5(raw_xml)
        agent_cfg = get_model_config(mas_config.agent_model) if mas_config.agent_model else None
        self.model = load_model(agent_cfg)
        if mas_config.agent_model:
            logger.info(f"[DigestAgent] Using Agent model config: {mas_config.agent_model}")
        else:
            logger.warning(
                "[DigestAgent] `agent_model` is not configured; DigestAgent is using default model config. "
                "Set `agent_model` in plugin config to reduce digest cost."
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
        if len(content) > _MAX_CONTENT_CHARS:
            content = content[:_MAX_CONTENT_CHARS] + "\n...(truncated)"

        text = self._normalize_text(f"{title} {content}")
        if len(text) < 40:
            return None

        prompt = (
            "Evaluate this article for Muika using the scoring guide and anchors "
            "provided in the system prompt.\n"
            "Return JSON with fields: score (0-100), keep (boolean), reason (short), "
            "primary_theme, summary.\n\n"
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

    async def fetch_and_digest(self) -> None:  # pragma: no cover
        """尝试抓取并生成一篇未读新闻的摘要。"""
        sources = [source for source in RSS_SOURCES.values() if source.digest]

        # 事件队列已饱和则跳过本周期，避免管道溢出
        if len(self.topic_manager._event_queue) >= _MAX_EVENT_QUEUE_SIZE:
            logger.debug(
                f"[DigestAgent] Event queue at capacity ({len(self.topic_manager._event_queue)}), "
                "skipping this cycle"
            )
            return

        async with get_session() as db_session:
            # 清理过期缓存条目
            expired_count = await RssDigestCacheCRUD.delete_expired(db_session, DIGEST_CACHE_TTL_DAYS)
            if expired_count > 0:
                logger.debug(f"[DigestAgent] Purged {expired_count} expired cache entries")

            for source in sources:
                try:
                    logger.debug(f"[DigestAgent] Fetching RSS: {source.name}")
                    rss_content = await fetch_web_content(source.url)

                    # 比对 RSS 指纹，内容未更新则跳过
                    raw_hash = hashlib.md5(rss_content).hexdigest()
                    if self._rss_fingerprints.get(source.id) == raw_hash:
                        logger.debug(f"[DigestAgent] RSS unchanged for {source.name}, skipping")
                        continue
                    self._rss_fingerprints[source.id] = raw_hash

                    entries = parse_rss_feed(rss_content)

                    entries = random.sample(entries, min(len(entries), _MAX_CANDIDATES_PER_SOURCE))

                    scored_candidates: list[ScoredCandidate] = []
                    for idx, entry in enumerate(entries):
                        if idx >= _MAX_EVALUATIONS_PER_SOURCE:
                            break

                        url_hash = hashlib.md5(entry.link.encode()).hexdigest()[:12]
                        topic_id = f"event_rss_{source.id}_{url_hash}"

                        # 检查是否已作为话题使用过
                        history = await TopicHistoryCRUD.get_by_topic_id(db_session, topic_id)
                        if history:
                            continue

                        # 检查评估缓存
                        cached = await RssDigestCacheCRUD.get_cached(db_session, topic_id, DIGEST_CACHE_TTL_DAYS)
                        if cached is not None:
                            if cached.keep == 0 or cached.score < DIGEST_MIN_SCORE:
                                logger.debug(
                                    f"[DigestAgent] Cached reject (score={cached.score}) "
                                    f"from {source.name}: {entry.title}"
                                )
                                continue
                            if not cached.summary.strip():
                                continue
                            # 使用缓存中的正面评估结果
                            scored_candidates.append(
                                ScoredCandidate(
                                    score=cached.score,
                                    entry=entry,
                                    topic_id=topic_id,
                                    summary=cached.summary.strip(),
                                    primary_theme=cached.primary_theme,
                                )
                            )
                            logger.debug(
                                f"[DigestAgent] Cache hit (score={cached.score}) " f"from {source.name}: {entry.title}"
                            )
                            continue

                        # 缓存未命中：抓取全文 + LLM 评估
                        # 提交以释放 SQLite 写锁，避免阻塞 @record_plugin_usage 的 usage 写入
                        await db_session.commit()

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

                        # 将所有评估结果写入缓存（避免重启后重复评估）
                        await RssDigestCacheCRUD.upsert(
                            session=db_session,
                            topic_id=topic_id,
                            source_id=source.id,
                            title=entry.title,
                            link=entry.link,
                            published=entry.published,
                            score=assessment.score,
                            keep=assessment.keep,
                            reason=assessment.reason,
                            primary_theme=assessment.primary_theme,
                            summary=assessment.summary.strip() if assessment.summary else "",
                        )

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

                    await TopicHistoryCRUD.record(db_session, topic_id=chosen.topic_id, user_engaged=False)

                    logger.success(f"[DigestAgent] Successfully digested and enqueued: {chosen.topic_id}")
                    return

                except Exception as e:
                    logger.error(f"[DigestAgent] Error fetching source {source.name}: {e}")

            logger.debug("[DigestAgent] No new unread entries found in checked sources.")
