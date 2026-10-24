from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, Field

from muika.core.state import MuikaState
from muika.plugin.func_call import on_function_call
from muika.utils.logger import logger

from .rss import extract_web_content

_CURIOSITY_GAIN = 0.15
"""成功读取网页后的好奇心增益；由时间衰减与话题管线消费。"""


class FetchWebContentParams(BaseModel):
    url: str = Field(..., description="The URL of the web content to fetch. Must be a valid http/https URL.")


@on_function_call(
    "Extract plain text content from a web page. Reading a page also satisfies a bit of your curiosity.",
    params=FetchWebContentParams,
)
async def fetch_web_content(url: str, state: MuikaState):
    """提取网页正文，拒绝不支持的 URL 协议。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Invalid URL {url!r}: only http/https is supported. "
            f"If no suitable tool exists for this task, report that directly."
        )

    logger.debug(f"[FetchWebContent] Fetching: {url}")
    content = await extract_web_content(url)
    state.curiosity = min(1.0, state.curiosity + _CURIOSITY_GAIN)
    return content
