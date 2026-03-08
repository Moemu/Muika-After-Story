from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from nonebot import logger
from pydantic import Field

from ..schema import ActionOutput
from ._base import BaseTool

if TYPE_CHECKING:
    from muika.core.executor import Executor
    from muika.core.state import MuikaState


class CaptureScreenshotTool(BaseTool):
    """Capture a screenshot and return it as an image resource."""

    name: Literal["capture_screenshot"] = "capture_screenshot"

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        from datetime import datetime
        from pathlib import Path
        from tempfile import gettempdir

        from PIL import ImageGrab

        from muika.models import Resource

        temp_dir = Path(gettempdir()) / "muika_screenshots"
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            screenshot = ImageGrab.grab()
        except OSError as e:
            return ActionOutput(
                content=f"[CaptureScreenshotTool] Screen capture failed (headless or no display available): {e}"
            )
        except Exception as e:
            logger.error(f"[CaptureScreenshotTool] Failed: {e}")
            return ActionOutput(content=f"[CaptureScreenshotTool] Failed to capture screenshot: {e}")

        try:
            screenshot.thumbnail((1920, 1080))
            timestamp = int(datetime.now().timestamp())
            file_path = temp_dir / f"screenshot_{timestamp}.png"
            screenshot.save(file_path)
            logger.info(f"[CaptureScreenshotTool] Saved to {file_path}")
            resource = Resource(type="image", path=str(file_path), mimetype="image/png")
            return ActionOutput(
                content=f"Screenshot captured successfully. Path: {file_path}",
                resources=[resource],
            )
        except Exception as e:
            logger.error(f"[CaptureScreenshotTool] Failed: {e}")
            return ActionOutput(content=f"[CaptureScreenshotTool] Failed to save screenshot: {e}")


class CaptureCameraPhotoTool(BaseTool):
    """Capture a photo from the webcam and return it as an image resource."""

    name: Literal["capture_camera_photo"] = "capture_camera_photo"
    device_index: int = Field(0, description="Camera device index (0 = default webcam).")

    async def handle(self, state: "MuikaState", executor: "Executor") -> ActionOutput:
        import asyncio
        from datetime import datetime
        from pathlib import Path
        from tempfile import gettempdir

        try:
            import cv2
        except ImportError:
            return ActionOutput(
                content="[CaptureCameraPhotoTool] opencv-python is not installed. " "Run: pip install opencv-python"
            )

        from muika.models import Resource

        def _capture(device_index: int) -> str:
            cap = cv2.VideoCapture(device_index)
            if not cap.isOpened():
                raise RuntimeError(f"Cannot open camera device {device_index}")
            try:
                ret, frame = cap.read()
                if not ret or frame is None:
                    raise RuntimeError("Failed to read frame from camera")
                temp_dir = Path(gettempdir()) / "muika_camera"
                temp_dir.mkdir(parents=True, exist_ok=True)
                timestamp = int(datetime.now().timestamp())
                file_path = temp_dir / f"camera_{timestamp}.jpg"
                cv2.imwrite(str(file_path), frame)
                return str(file_path)
            finally:
                cap.release()

        try:
            loop = asyncio.get_event_loop()
            file_path = await loop.run_in_executor(None, _capture, self.device_index)
            logger.info(f"[CaptureCameraPhotoTool] Saved to {file_path}")
            resource = Resource(type="image", path=file_path, mimetype="image/jpeg")
            return ActionOutput(
                content=f"Camera photo captured successfully. Path: {file_path}",
                resources=[resource],
            )
        except Exception as e:
            logger.error(f"[CaptureCameraPhotoTool] Failed: {e}")
            return ActionOutput(content=f"Failed to capture camera photo: {e}")
