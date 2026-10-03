"""把具名阅读和用量业务操作提交到权威数据库。"""

from sqlalchemy.ext.asyncio import AsyncSession

from muika.database.activity import (
    ActivityOperation,
    ActivityResult,
    ReadingCache,
    TopicHistory,
    UsageRecord,
)
from muika.database.crud import RssDigestCacheCRUD, TopicHistoryCRUD, UsageORM


async def execute_activity(db: AsyncSession, action: ActivityOperation) -> ActivityResult:
    result = ActivityResult()
    if action.action == "topic_get":
        topic = await TopicHistoryCRUD.get_by_topic_id(db, action.topic_id)
        if topic:
            result.topics = [TopicHistory.model_validate(topic)]
    elif action.action == "topic_used":
        topic_row = await TopicHistoryCRUD.record(db, action.topic_id, action.user_engaged)
        result.topics = [TopicHistory.model_validate(topic_row)]
    elif action.action == "topics":
        result.topics = [TopicHistory.model_validate(row) for row in await TopicHistoryCRUD.list_all(db, action.limit)]
    elif action.action in {"reading_get", "reading_prune"}:
        if action.days is None:
            raise ValueError("Reading cache retention requires a finite day count.")
        if action.action == "reading_get":
            row = await RssDigestCacheCRUD.get_cached(db, action.topic_id, action.days)
            result.reading = ReadingCache.model_validate(row) if row else None
        else:
            result.deleted = await RssDigestCacheCRUD.delete_expired(db, action.days)
    elif action.action == "reading_save":
        if action.reading is None:
            raise ValueError("Reading cache content is required.")
        reading = action.reading
        row = await RssDigestCacheCRUD.upsert(
            db,
            reading.topic_id,
            reading.source_id,
            reading.title,
            reading.link,
            reading.published,
            reading.score,
            bool(reading.keep),
            reading.reason,
            reading.primary_theme,
            reading.summary,
        )
        result.reading = ReadingCache.model_validate(row)
    elif action.action == "usage_save":
        if action.usage is None:
            raise ValueError("Model usage is required.")
        usage = action.usage
        await UsageORM.save_usage(
            db, usage.plugin, usage.model, usage.input_tokens, usage.output_tokens, usage.cached_tokens, usage.type
        )
    else:
        result.usage = [UsageRecord.model_validate(row) for row in await UsageORM.get_usage_records(db, action.days)]
    return result
