from nonebot_plugin_orm import Model
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column


class Usage(Model):
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    plugin: Mapped[str] = mapped_column(String)
    type: Mapped[str] = mapped_column(String, nullable=False)
    date: Mapped[str] = mapped_column(String, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=True, default=0)
