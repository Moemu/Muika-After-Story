from dataclasses import dataclass


@dataclass
class RSSSource:
    id: str
    name: str
    url: str
    language: str
    topics: list[str]
    digest: bool = False
