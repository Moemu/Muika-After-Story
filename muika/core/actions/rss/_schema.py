from dataclasses import dataclass
from datetime import datetime


@dataclass
class CheckRSSUpdatePayload:
    source_id: str


@dataclass
class RSSOutline:
    id: str
    title: str
    link: str
    published: datetime
    source: str


@dataclass
class CheckRSSResult:
    items: list[RSSOutline]


@dataclass
class FetchRSSContentPayload:
    source: str
    item_id: str


@dataclass
class RSSContent:
    title: str
    content: str
    published: datetime


@dataclass
class RSSSource:
    id: str
    name: str
    url: str
    language: str
    topics: list[str]
    update_interval: int
