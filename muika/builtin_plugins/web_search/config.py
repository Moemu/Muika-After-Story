"""联网搜索插件配置。"""

from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebSearchConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WEB_SEARCH_", env_file=".env", extra="ignore")

    provider: Literal["tavily", "perplexity", "deepseek"] | None = None
    api_key: str = ""

    @field_validator("provider", mode="before")
    @classmethod
    def normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower() or None
        return value


plugin_config = WebSearchConfig()
