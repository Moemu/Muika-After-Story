from datetime import datetime
from typing import Literal, Optional

from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import func, select

from .orm_models import Usage


class UsageORM:
    @staticmethod
    async def get_usage(
        session: async_scoped_session,
        plugin: Optional[str],
        date: Optional[str],
        type: Optional[Literal["chat", "embedding"]] = None,
    ) -> int:
        """
        获取用量信息

        :param session: 数据库会话
        :param plugin: (可选)插件名称，如果为 None 则返回所有插件的用量
        :param date: (可选)日期(`%Y.%m.%d`)，如果为 None 则返回所有日期的用量
        :param type: (可选)用量类型，默认为 None，表示返回所有类型的用量
        """
        query = select(func.sum(Usage.tokens))
        if plugin:
            query = query.where(Usage.plugin == plugin)
        if date:
            query = query.where(Usage.date.like(date))
        if type:
            query = query.where(Usage.type == type)
        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def save_usage(
        session: async_scoped_session, plugin: str, total_tokens: int, type: Literal["chat", "embedding"] = "chat"
    ):
        """
        保存用量信息
        """
        if total_tokens < 0:
            return

        date = datetime.now().strftime("%Y.%m.%d")
        stmt = await session.execute(
            select(Usage).where(Usage.plugin == plugin, Usage.type == type, Usage.date == date).limit(1)
        )
        usage = stmt.scalar_one_or_none()

        if usage is not None:
            usage.tokens += total_tokens
            return

        session.add(Usage(plugin=plugin, type=type, date=date, tokens=total_tokens))
