from datetime import datetime
from pathlib import Path
from tempfile import gettempdir

from nonebot import logger
from PIL import ImageGrab

from muika.models import Resource

from ...actions import ActionOutput
from ..registry import register_tool
from ..tools import CaptureScreenshotTool

TEMP_DIR = Path(gettempdir()) / "muika_screenshots"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


@register_tool("capture_screenshot")
async def handle_capture_screenshot(tool: CaptureScreenshotTool) -> ActionOutput:
    try:
        screenshot = ImageGrab.grab()
        screenshot.thumbnail((1920, 1080))

        timestamp = int(datetime.now().timestamp())
        file_path = TEMP_DIR / f"screenshot_{timestamp}.png"
        screenshot.save(file_path)

        logger.info(f"Screenshot captured and saved to {file_path}")

        resource = Resource(type="image", path=str(file_path), mimetype="image/png")

        return ActionOutput(content=f"Screenshot captured successfully. Path: {file_path}", resources=[resource])
    except Exception as e:
        logger.error(f"Failed to capture screenshot: {e}")
        return ActionOutput(content=f"Failed to capture screenshot: {str(e)}")
