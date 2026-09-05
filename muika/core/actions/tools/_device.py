from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import gettempdir

from PIL import ImageGrab
from pydantic import BaseModel, Field

from muika.models import Resource
from muika.plugin.func_call import on_function_call
from muika.plugin.func_call.context import ToolContext
from muika.utils.logger import logger

try:
    import cv2
except ImportError:
    cv2 = None


@on_function_call("Capture a screenshot and return it as an image resource.")
async def capture_screenshot(context: ToolContext) -> str:
    """截取屏幕并将图片加入本次工具调用的资源。"""
    temp_dir = Path(gettempdir()) / "muika_screenshots"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        screenshot = ImageGrab.grab()
    except OSError as e:
        return f"Screen capture failed (headless or no display available): {e}"
    except Exception as e:
        logger.error(f"[CaptureScreenshot] Failed: {e}")
        return f"Failed to capture screenshot: {e}"

    try:
        screenshot.thumbnail((1920, 1080))
        timestamp = int(datetime.now().timestamp())
        file_path = temp_dir / f"screenshot_{timestamp}.png"
        screenshot.save(file_path)
        logger.info(f"[CaptureScreenshot] Saved to {file_path}")
        resource = Resource(type="image", path=str(file_path), mimetype="image/png")
        context.resources.append(resource)
        return "Screenshot captured successfully. See attached image."
    except Exception as e:
        logger.error(f"[CaptureScreenshot] Failed: {e}")
        return f"Failed to save screenshot: {e}"


class CaptureCameraPhotoParams(BaseModel):
    device_index: int = Field(0, description="Camera device index (0 = default webcam).")


@on_function_call(
    "Capture a photo from the webcam and return it as an image resource.",
    params=CaptureCameraPhotoParams,
)
async def capture_camera_photo(context: ToolContext, device_index: int = 0) -> str:
    """从指定摄像头拍照并附加图片资源。"""
    if cv2 is None:
        return "opencv-python is not installed. Run: pip install opencv-python"

    def _capture(idx: int) -> str:
        cap = cv2.VideoCapture(idx)
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open camera device {idx}")
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
        file_path = await loop.run_in_executor(None, _capture, device_index)
        logger.info(f"[CaptureCameraPhoto] Saved to {file_path}")
        resource = Resource(type="image", path=file_path, mimetype="image/jpeg")
        context.resources.append(resource)
        return "Camera photo captured successfully. See attached image."
    except Exception as e:
        logger.error(f"[CaptureCameraPhoto] Failed: {e}")
        return f"Failed to capture camera photo: {e}"
