from datetime import datetime
from pathlib import Path
from tempfile import gettempdir

from nonebot import logger
from PIL import ImageGrab

from muika.models import Resource

from ...intents import CaptureScreenshotIntent
from .._registry import ActionOutput, register_action

TEMP_DIR = Path(gettempdir()) / "muika_screenshots"
TEMP_DIR.mkdir(parents=True, exist_ok=True)


@register_action("capture_screenshot")
async def handle_capture_screenshot(intent: CaptureScreenshotIntent) -> ActionOutput:
    try:
        # Capture the primary screen
        screenshot = ImageGrab.grab()

        # Resize if too large (optional, but good for token saving if we send base64)
        # For now, keep original or resize to max 1920x1080 to be safe
        screenshot.thumbnail((1920, 1080))

        # Save to temp file
        timestamp = int(datetime.now().timestamp())
        file_path = TEMP_DIR / f"screenshot_{timestamp}.png"
        screenshot.save(file_path)

        logger.info(f"Screenshot captured and saved to {file_path}")

        # Return a message indicating success and the path.
        # The LLM prompt builder should ideally pick this up and attach the image.

        resource = Resource(type="image", path=str(file_path), mimetype="image/png")

        return ActionOutput(content=f"Screenshot captured successfully. Path: {file_path}", resources=[resource])
    except Exception as e:
        logger.error(f"Failed to capture screenshot: {e}")
        return ActionOutput(content=f"Failed to capture screenshot: {str(e)}")
