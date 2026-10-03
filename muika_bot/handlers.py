"""Muika Bot message handlers.

Handles user messages, multimodal resource extraction, command forwarding,
and lifecycle management.  Always communicates with the Core process via IPC.
"""

import asyncio
import hashlib
import json
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
from nonebot_plugin_alconna.uniseg import get_target

from muika.config import mas_config
from muika.ipc.bot_client import DeliveryNotStarted
from muika.models import Resource
from muika.node.config import NodeProfile
from muika.utils.logger import logger

from .first_run import require_user_agreement
from .ipc_client import IpcClient
from .node_client import NodeBotIpcClient
from .session import SessionManager
from .utils.utils import download_file, get_file_via_adapter

DELAYED_SECOND_PER_PARAGRAPH = 3

driver = get_driver()
session_manager = SessionManager()

_ipc_client: IpcClient = (
    NodeBotIpcClient(NodeProfile.read(mas_config.node_profile))
    if mas_config.node_profile is not None
    else IpcClient(core_url=mas_config.core_ws_url, secret=mas_config.ipc_secret)
)
_message_target = Target(id=mas_config.master_id, private=True)


async def _is_master(event: Event) -> bool:
    """Rule: only respond to the configured master user."""
    if event.get_type() != "message":
        return False
    try:
        return event.get_user_id() == mas_config.master_id
    except (AttributeError, NotImplementedError, ValueError):
        return False


_master_rule = Rule(_is_master)


async def _render_resources(resources: list[dict], target: Target | None = None) -> None:
    """将多模态资源列表渲染为 UniMessage 发回用户。"""
    for i, res in enumerate(resources):
        res_type = res.get("type", "")
        path = res.get("path", "")
        if not path:
            continue
        if res_type == "image":
            await UniMessage.image(path=path).send(target=target or _message_target, bot=_target_bot(target))
        elif res_type in ("audio", "video"):
            await UniMessage(path).send(target=target or _message_target, bot=_target_bot(target))
        else:
            await UniMessage.file(path=path).send(target=target or _message_target, bot=_target_bot(target))
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
                path = await get_file_via_adapter(resource.origin, event)  # type: ignore
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


def _target_bot(target: Target | None) -> Bot:
    try:
        return get_bot(target.self_id if target else None)
    except (ValueError, KeyError) as exc:
        raise DeliveryNotStarted("The original platform Bot is not connected.") from exc


def _reply_target(data: dict) -> Target:
    if isinstance(_ipc_client, NodeBotIpcClient):
        try:
            return Target.load(json.loads(_ipc_client.durable.spool.route(data["conversation_id"])))
        except ValueError as exc:
            raise DeliveryNotStarted("The original conversation route is not ready.") from exc
    return _message_target


async def _send_message(message: str, target: Target | None = None) -> None:
    """发送 Core 提供的消息段或完整命令结果。"""
    await UniMessage(message).send(target=target or _message_target, bot=_target_bot(target))
    await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)


def _init_ipc_client() -> IpcClient:
    """Initialize the IPC client and register Core -> Bot message handlers."""

    @_ipc_client.on_message("send_message")
    async def _handle_send_message(data: dict) -> None:
        target = _reply_target(data)
        content = data.get("content", "")
        if content:
            await _send_message(content, target)
        resources = data.get("resources", [])
        if resources:
            await _render_resources(resources, target)

    @_ipc_client.on_message("command_result")
    async def _handle_command_result(data: dict) -> None:
        target = _reply_target(data)
        content = data.get("content", "")
        if content:
            await _send_message(content, target)
        resources = data.get("resources", [])
        if resources:
            await _render_resources(resources, target)

    @_ipc_client.on_message("action_response")
    async def _handle_action_response(data: dict) -> None:
        logger.debug(f"Received Action Response: {data}")

    @_ipc_client.on_message("error")
    async def _handle_error(data: dict) -> None:
        logger.error(f"[IPC] Core error: {data.get('message', 'Unknown')}")

    return _ipc_client


async def _get_ipc_client() -> IpcClient:
    if not _ipc_client.is_connected and not isinstance(_ipc_client, NodeBotIpcClient):
        await UniMessage("IPC 进程未连接").finish()

    return _ipc_client


@driver.on_startup
async def startup() -> None:
    """Bot startup: connect to Core, load plugins."""
    logger.info("Starting chat service...")
    require_user_agreement()

    logger.debug(f"Connecting to Core ({mas_config.core_ws_url})...")
    client = _init_ipc_client()
    asyncio.create_task(client.connect())
    connected = await client.wait_connected(timeout=10.0)
    if connected:
        logger.debug("Connected to Core process")
    else:
        logger.warning("Core connection timed out, messages will be queued")

    logger.success("Chat service is ready.")


def _detect_adapter_type() -> str:
    """
    从 NoneBot adapter 元数据推断适配器名称和类型。
    """
    configured_name = mas_config.client_name
    if configured_name:
        return configured_name

    try:
        bot = get_bot()
        adapter_name = bot.adapter.get_name()

        return adapter_name or "nonebot2"
    except Exception:
        logger.warning("[Handler] Failed to detect adapter type — using defaults")
        return "nonebot2"


@driver.on_bot_connect
async def bot_connected() -> None:
    """Handle Bot platform connection."""
    logger.success("Chat platform connected.")

    # 检测并设置适配器身份
    client_name = _detect_adapter_type()
    _ipc_client.set_client_info(client_name)
    if isinstance(_ipc_client, NodeBotIpcClient):
        bot = get_bot()
        _ipc_client.durable.spool.save_route(
            "master",
            json.dumps(
                Target(
                    id=mas_config.master_id, private=True, self_id=bot.self_id, adapter=bot.adapter.get_name()
                ).dump(),
                ensure_ascii=False,
            ),
        )

    if _ipc_client.is_connected:
        logger.debug("[Bootstrap] bot_connected event sent via IPC.")
    else:
        logger.warning("[Bootstrap] Core not connected -- bootstrap event queued.")

    await _ipc_client.send_session_bootstrap()


@driver.on_shutdown
async def shutdown() -> None:
    """停止接入重连并保留尚未投递的队列。"""
    await _ipc_client.disconnect()


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
    conversation_id = "master"
    message_id = None
    if isinstance(ipc_client, NodeBotIpcClient):
        session_id = event.get_session_id()
        conversation_id = hashlib.sha256((bot.self_id + ":" + session_id).encode()).hexdigest()
        message_id = hashlib.sha256((conversation_id + ":" + str(get_message_id(event, bot))).encode()).hexdigest()
        ipc_client.durable.spool.save_route(
            conversation_id, json.dumps(get_target(event, bot).dump(), ensure_ascii=False)
        )
    if any((bot_message.startswith("."), bot_message.startswith("/"))):
        raw = event.get_plaintext()
        await ipc_client.send_command(raw, message_id=message_id, conversation_id=conversation_id)
        if (
            isinstance(ipc_client, NodeBotIpcClient)
            and not ipc_client.durable.core_available
            and not ipc_client.offline_notice_sent
        ):
            ipc_client.offline_notice_sent = True
            await UniMessage("[System] 暂时无法联系 Muika。命令已保存，会在恢复后送达。").send(
                target=get_target(event, bot), bot=bot
            )
        return

    if message_reply := ext.get_reply(get_message_id(event, bot)):
        reply_message = message_reply.msg
        if isinstance(reply_message, BotMessage):
            bot_message += UniMessage("\n被引用的消息: ") + UniMessage(reply_message)
        else:
            bot_message += UniMessage(f"\n被引用的消息: {reply_message}")

    merged_message = (
        bot_message
        if isinstance(ipc_client, NodeBotIpcClient)
        else await session_manager.put_and_wait(event, bot_message)
    )
    if not merged_message:
        matcher.skip()
        return

    message_text = merged_message.extract_plain_text()
    message_resource = await _extract_multi_resources(merged_message, event)

    logger.debug(f"Received message: {message_text} multimodal: {message_resource}")

    if not any((message_text, message_resource)):
        return

    await ipc_client.send_user_message(
        message_text,
        resources=[r.to_dict() for r in message_resource],
        message_id=message_id,
        conversation_id=conversation_id,
    )
    if (
        isinstance(ipc_client, NodeBotIpcClient)
        and not ipc_client.durable.core_available
        and not ipc_client.offline_notice_sent
    ):
        ipc_client.offline_notice_sent = True
        await UniMessage("[System] 暂时无法联系 Muika。消息已保存，会在恢复后送达。").send(
            target=get_target(event, bot), bot=bot
        )
