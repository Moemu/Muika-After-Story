"""Muika Bot message handlers.

Handles user messages, multimodal resource extraction, command forwarding,
and lifecycle management.  Always communicates with the Core process via IPC.
"""

import asyncio
import os
import re
import time
from typing import Literal
from urllib.parse import urlparse

from arclet.alconna import Alconna, AllParam, Args
from nonebot import get_bot, get_driver
from nonebot.adapters import Bot, Event
from nonebot.adapters import Message as BotMessage
from nonebot.matcher import Matcher
from nonebot.params import Depends
from nonebot.rule import Rule, to_me
from nonebot_plugin_alconna import (
    Target,
    UniMessage,
    UniMsg,
    get_message_id,
    on_alconna,
    uniseg,
)
from nonebot_plugin_alconna.builtins.extensions import ReplyRecordExtension

from muika.config import mas_config
from muika.models import Resource
from muika.utils.logger import logger

from .first_run import user_agreement
from .ipc_client import IpcClient
from .session import SessionManager
from .utils.utils import download_file, get_file_via_adapter

COMMON_PUNCTUATION = "。！？；…\n"
DELAYED_SECOND_PER_PARAGRAPH = 3

driver = get_driver()
session_manager = SessionManager()

_ipc_client: IpcClient = IpcClient(core_url=mas_config.core_ws_url, secret=mas_config.ipc_secret)
_message_target = Target(id=mas_config.master_id, private=True)


async def _is_master(event: Event) -> bool:
    """Rule: only respond to the configured master user."""
    try:
        return event.get_user_id() == mas_config.master_id
    except (AttributeError, NotImplementedError):
        return False


_master_rule = Rule(_is_master)


async def _render_resources(resources: list[dict]) -> None:
    """将多模态资源列表渲染为 UniMessage 发回用户。"""
    for i, res in enumerate(resources):
        res_type = res.get("type", "")
        path = res.get("path", "")
        if not path:
            continue
        if res_type == "image":
            await UniMessage.image(path=path).send(target=_message_target, bot=get_bot())
        elif res_type in ("audio", "video"):
            await UniMessage(path).send(target=_message_target, bot=get_bot())
        else:
            await UniMessage.file(path=path).send(target=_message_target, bot=get_bot())
        if i < len(resources) - 1:
            await asyncio.sleep(0.3)


def _get_media_filename(media: uniseg.segment.Media, type: Literal["audio", "image", "video", "file"]) -> str:
    """Generate a unique filename for a multimodal media segment."""
    _default_suffix = {"audio": "mp3", "image": "png", "video": "mp4", "file": ""}
    assert media.url
    if media.name:
        file_suffix = media.name.split(".")[-1] if media.name.count(".") else _default_suffix[type]
    else:
        path = urlparse(media.url).path
        _, ext = os.path.splitext(path)
        file_suffix = ext.lstrip(".") if ext else _default_suffix[type]
    return f"{time.time_ns()}.{file_suffix}"


async def _extract_multi_resource(
    message: UniMessage, type: Literal["audio", "image", "video", "file"], event: Event
) -> list[Resource]:
    """Extract a single type of multimodal resource from a message."""
    resources = []
    for resource in message:
        assert isinstance(resource, uniseg.segment.Media)
        try:
            if resource.path is not None:
                path = str(resource.path)
            elif resource.url is not None:
                path = await download_file(resource.url, file_name=_get_media_filename(resource, type))
            elif resource.origin is not None:
                logger.warning("Cannot get file URL via generic method, falling back to adapter...")
                path = await get_file_via_adapter(resource.origin, event)  # type:ignore
            else:
                continue
            if path:
                resources.append(Resource(type, path=path))
        except Exception as e:
            logger.error(f"Failed to process file: {e}")
    return resources


async def _extract_multi_resources(message: UniMsg, event: Event) -> list[Resource]:
    """Extract all multimodal resources from a message."""
    resources = []
    message_audio = message.get(uniseg.Audio) + message.get(uniseg.Voice)
    message_images = message.get(uniseg.Image)
    message_file = message.get(uniseg.File)
    message_video = message.get(uniseg.Video)
    resources.extend(await _extract_multi_resource(message_audio, "audio", event))
    resources.extend(await _extract_multi_resource(message_file, "file", event))
    resources.extend(await _extract_multi_resource(message_images, "image", event))
    resources.extend(await _extract_multi_resource(message_video, "video", event))
    return resources


def _split_message(content: str, max_length_per_message: int = 250) -> list[str]:
    """将消息按自然边界切分，贪心合并以最小化切出的消息段数量。"""
    paragraphs = content.split("\n\n")
    final_messages = []

    for paragraph in paragraphs:
        if len(paragraph) <= max_length_per_message:
            final_messages.append(paragraph)
            continue

        # 先按标点切分为自然句段
        segments = []
        current = ""
        for char in paragraph:
            current += char
            if char in COMMON_PUNCTUATION:
                segments.append(current)
                current = ""
        if current:
            segments.append(current)

        # 贪心合并句段，使每条消息尽可能接近 max_length_per_message
        buffer = ""
        for seg in segments:
            if len(buffer) + len(seg) <= max_length_per_message:
                buffer += seg
            else:
                if buffer:
                    final_messages.append(buffer)
                    buffer = ""
                # 若单个句段超过上限，硬切分
                while len(seg) > max_length_per_message:
                    final_messages.append(seg[:max_length_per_message])
                    seg = seg[max_length_per_message:]
                buffer = seg
        if buffer:
            final_messages.append(buffer)

    return final_messages


async def _send_message(message: str, raw: bool = False):
    """
    发送消息给用户
    """
    if not raw:
        # 移除 agent 指令会导致 4 个同时出现的换行符，要么替换为 2 个，要么提示用户
        message = message.strip().replace("\n\n\n\n", "\n\n")
        messages = _split_message(message)
    else:
        messages = [message]
    for msg in messages:
        await UniMessage(msg).send(target=_message_target, bot=get_bot())
        await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)


def _init_ipc_client() -> IpcClient:
    """Initialize the IPC client and register Core -> Bot message handlers."""

    @_ipc_client.on_message("send_message")
    async def _handle_send_message(data: dict) -> None:
        content = data.get("content", "")
        if content:
            await _send_message(content)
        resources = data.get("resources", [])
        if resources:
            await _render_resources(resources)

    @_ipc_client.on_message("command_result")
    async def _handle_command_result(data: dict) -> None:
        content = data.get("content", "")
        if content:
            await _send_message(content, raw=True)
        resources = data.get("resources", [])
        if resources:
            await _render_resources(resources)

    @_ipc_client.on_message("action_response")
    async def _handle_action_response(data: dict) -> None:
        logger.debug(f"Received Action Response: {data}")

    @_ipc_client.on_message("error")
    async def _handle_error(data: dict) -> None:
        logger.error(f"[IPC] Core error: {data.get('message', 'Unknown')}")

    return _ipc_client


async def _get_ipc_client() -> IpcClient:
    if not _ipc_client.is_connected:
        await UniMessage("IPC 进程未连接").finish()

    return _ipc_client


@driver.on_startup
async def startup() -> None:
    """Bot startup: connect to Core, load plugins."""
    logger.info("Loading MAS framework...")
    user_agreement.check_first_run()

    logger.info(f"Connecting to Core ({mas_config.core_ws_url})...")
    client = _init_ipc_client()
    asyncio.create_task(client.connect())
    connected = await client.wait_connected(timeout=10.0)
    if connected:
        logger.success("Connected to Core process")
    else:
        logger.warning("Core connection timed out, messages will be queued")

    logger.success("MAS framework is ready")


@driver.on_bot_connect
async def bot_connected() -> None:
    """Handle Bot platform connection."""
    logger.success("Bot connected")

    if _ipc_client.is_connected:
        logger.info("[Bootstrap] bot_connected event sent via IPC.")
    else:
        logger.warning("[Bootstrap] Core not connected -- bootstrap event queued.")

    await _ipc_client.send_session_bootstrap()


at_event = on_alconna(
    Alconna(re.compile(".+"), Args["text?", AllParam], separators=""),
    priority=100,
    rule=to_me() & _master_rule,
    block=True,
    extensions=[ReplyRecordExtension()],
)


@at_event.handle()
async def handle_supported_adapters(
    bot_message: UniMsg,
    event: Event,
    bot: Bot,
    matcher: Matcher,
    ext: ReplyRecordExtension,
    ipc_client: IpcClient = Depends(_get_ipc_client),
) -> None:
    """Main message handler -- receives user messages and forwards to Core."""
    if any((bot_message.startswith("."), bot_message.startswith("/"))):
        raw = event.get_plaintext()
        await ipc_client.send_command(raw)
        return

    if message_reply := ext.get_reply(get_message_id(event, bot)):
        reply_message = message_reply.msg
        if isinstance(reply_message, BotMessage):
            bot_message += UniMessage("\n被引用的消息: ") + UniMessage(reply_message)
        else:
            bot_message += UniMessage(f"\n被引用的消息: {reply_message}")

    if not (merged_message := await session_manager.put_and_wait(event, bot_message)):
        matcher.skip()
        return

    message_text = merged_message.extract_plain_text()
    message_resource = await _extract_multi_resources(merged_message, event)

    logger.info(f"Received message: {message_text} multimodal: {message_resource}")

    if not any((message_text, message_resource)):
        return

    await ipc_client.send_user_message(
        message_text,
        resources=[r.to_dict() for r in message_resource],
    )
