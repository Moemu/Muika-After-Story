"""在传输边界使用 UTC，业务层继续使用本机的本地时间。"""

from collections.abc import Callable
from datetime import date, datetime, timezone
from typing import Annotated, Any, TypeVar

from pydantic import AfterValidator, BaseModel, PlainSerializer

Model = TypeVar("Model", bound=BaseModel)


def local_time(value: datetime) -> datetime:
    return value.astimezone().replace(tzinfo=None) if value.tzinfo else value


def utc_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def local_time_text(value: str) -> str:
    return local_time(datetime.fromisoformat(value)).isoformat() if value else value


def utc_time_text(value: str) -> str:
    return utc_time(datetime.fromisoformat(value)) if value else value


def map_model_times(value: Model, convert: Callable[[datetime], datetime]) -> Model:
    """转换已声明的 datetime 字段，不猜测普通文本或日期的含义。"""

    def visit(item: Any) -> Any:
        if isinstance(item, datetime):
            return convert(item)
        if isinstance(item, BaseModel):
            return item.model_copy(update={name: visit(child) for name, child in item.__dict__.items()})
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            return {name: visit(child) for name, child in item.items()}
        return item

    return visit(value)


def local_model_times(value: Model) -> Model:
    return map_model_times(value, local_time)


def serialize_model_times(value: BaseModel) -> dict:
    def visit(item: Any) -> Any:
        if isinstance(item, datetime):
            return utc_time(item)
        if isinstance(item, date):
            return item.isoformat()
        if isinstance(item, list):
            return [visit(child) for child in item]
        if isinstance(item, dict):
            return {name: visit(child) for name, child in item.items()}
        return item

    return visit(value.model_dump(mode="python"))


LocalTime = Annotated[datetime, AfterValidator(local_time), PlainSerializer(utc_time, when_used="json")]
LocalTimeText = Annotated[str, AfterValidator(local_time_text), PlainSerializer(utc_time_text, when_used="json")]
WireTimeModel = Annotated[
    Model, AfterValidator(local_model_times), PlainSerializer(serialize_model_times, when_used="json")
]
