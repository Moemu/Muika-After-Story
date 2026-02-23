from typing import Annotated, Literal, Optional, TypeAlias, Union

from pydantic import BaseModel, Field

from .handlers.rss import AVAILABLE_RSS_SOURCES


class BaseTool(BaseModel):
    pass


class CheckRSSUpdateTool(BaseTool):
    """Check for updates from an RSS source."""

    name: Literal["check_rss_update"] = "check_rss_update"
    rss_source: str = Field(
        ...,
        description=f"RSS source identifier. Available sources: {AVAILABLE_RSS_SOURCES}",
    )


class FetchWebContentTool(BaseTool):
    """Fetch content from a URL."""

    name: Literal["fetch_web_content"] = "fetch_web_content"
    url: str = Field(
        ...,
        description="The URL of the web content to fetch.",
    )


class CaptureScreenshotTool(BaseTool):
    """Capture a screenshot of the current screen."""

    name: Literal["capture_screenshot"] = "capture_screenshot"


class ListProcessesTool(BaseTool):
    """List running processes."""

    name: Literal["list_processes"] = "list_processes"
    filter: Optional[str] = Field(None, description="Optional filter string for process names.")
    limit: int = Field(50, description="Maximum number of processes to return.")
    offset: int = Field(0, description="Number of processes to skip for pagination.")


class GetFocusedWindowTool(BaseTool):
    """Get the title of the currently focused window."""

    name: Literal["get_focused_window"] = "get_focused_window"


class GetSystemStatusTool(BaseTool):
    """Get general system status (CPU, memory, etc.)."""

    name: Literal["get_system_status"] = "get_system_status"


PerceptionTool: TypeAlias = Annotated[
    Union[
        CheckRSSUpdateTool,
        FetchWebContentTool,
        CaptureScreenshotTool,
        ListProcessesTool,
        GetSystemStatusTool,
        GetFocusedWindowTool,
    ],
    Field(discriminator="name"),
]
