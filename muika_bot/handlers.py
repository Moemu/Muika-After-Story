"""Muika Bot message handlers.

Handles user messages, .model/.debug commands, multimodal resource extraction,
and lifecycle management.  Always communicates with the Core process via IPC.
"""

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from arclet.alconna import Alconna, AllParam, Args
from nonebot import get_bot, get_driver
from nonebot.adapters import Bot, Event
from nonebot.adapters import Message as BotMessage
from nonebot.matcher import Matcher
from nonebot.params import Depends
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from nonebot_plugin_alconna import (
    AlconnaMatch,
    CommandMeta,
    Match,
    MsgTarget,
    Subcommand,
    Target,
    UniMessage,
    UniMsg,
    get_message_id,
    on_alconna,
    uniseg,
)
from nonebot_plugin_alconna.builtins.extensions import ReplyRecordExtension
from nonebot_plugin_session import SessionIdType, extract_session

from muika.config import get_model_config_manager, mas_config
from muika.models import Resource
from muika.plugin import load_plugins
from muika.plugin.mcp import initialize_servers
from muika.utils.logger import logger

from .first_run import user_agreement
from .ipc_client import IpcClient
from .session import SessionManager
from .utils.utils import download_file, get_file_via_adapter

COMMAND_PREFIXES = [".", "/"]
PLUGINS_PATH = Path("./plugins")
MCP_CONFIG_PATH = Path("./configs/mcp.json")
COMMON_PUNCTUATION = "。！？；…\n"
DELAYED_SECOND_PER_PARAGRAPH = 3

driver = get_driver()
session_manager = SessionManager()

_ipc_client: IpcClient = IpcClient(core_url=mas_config.core_ws_url, secret=mas_config.ipc_secret)
_message_target = Target(id=mas_config.master_id, private=True)


async def _get_ipc_client() -> IpcClient:
    if not _ipc_client.is_connected:
        await UniMessage("IPC 进程未连接").finish()

    return _ipc_client


def _init_ipc_client() -> IpcClient:
    """Initialize the IPC client and register Core -> Bot message handlers."""

    @_ipc_client.on_message("send_message")
    async def _handle_send_message(data: dict) -> None:
        content = data.get("content", "")
        if not content:
            return
        await _send_message(content)

    @_ipc_client.on_message("action_response")
    async def _handle_action_response(data: dict) -> None:
        logger.debug(f"Received Action Response: {data}")

    @_ipc_client.on_message("query_response")
    async def _handle_state_update(data: dict) -> None:
        state = data.get("data")
        if not state:
            return
        message = _format_state_display(state)
        await _send_message(message)

    @_ipc_client.on_message("error")
    async def _handle_error(data: dict) -> None:
        logger.error(f"[IPC] Core error: {data.get('message', 'Unknown')}")

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

    logger.info("Loading MAS plugins...")
    if PLUGINS_PATH.exists():
        logger.info("Loading external plugins...")
        load_plugins("./plugins")

    if MCP_CONFIG_PATH.exists():
        logger.info("Loading MCP Server config")
        await initialize_servers()

    logger.success("Plugin loading complete")
    logger.success("MAS framework is ready")


@driver.on_bot_connect
async def bot_connected() -> None:
    """Handle Bot platform connection."""
    logger.success("Bot connected")

    if _ipc_client.is_connected:
        logger.info("[Bootstrap] bot_connected event sent via IPC.")
    else:
        logger.warning("[Bootstrap] Core not connected -- bootstrap event queued.")

    await _ipc_client.send_event("bot_connected")


at_event = on_alconna(
    Alconna(re.compile(".+"), Args["text?", AllParam], separators=""),
    priority=100,
    rule=to_me(),
    block=True,
    extensions=[ReplyRecordExtension()],
)

command_model = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "model",
        Subcommand("help", help_text="显示指令帮助"),
        Subcommand("load", Args["config_name?", str], help_text="切换指定模型配置"),
        Subcommand("reload", help_text="重新加载模型配置文件"),
        Subcommand("list", help_text="列出所有可用模型配置"),
        meta=CommandMeta("Muika model config management"),
    ),
    priority=10,
    block=True,
    skip_for_unmatch=False,
    permission=SUPERUSER,
)

command_debug = on_alconna(
    Alconna(
        COMMAND_PREFIXES,
        "debug",
        Subcommand("topic", help_text="立即触发一次话题旁路管线"),
        Subcommand("state", help_text="显示当前 Muika 情绪状态"),
        Subcommand(
            "state-set",
            Args["field", str]["value", str],
            help_text="修改情绪状态字段",
        ),
        Subcommand("topic-reset", help_text="清空当前活跃话题"),
        meta=CommandMeta("Muika debug commands"),
    ),
    priority=10,
    block=True,
    skip_for_unmatch=False,
    permission=SUPERUSER,
)


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
    messages_split_by_newlines = content.split("\n\n")
    final_messages = []
    for msg in messages_split_by_newlines:
        if len(msg) <= max_length_per_message:
            final_messages.append(msg)
            continue
        messages_spilt_by_punctuation = []
        current_segment = ""
        for char in msg:
            current_segment += char
            if char in COMMON_PUNCTUATION:
                messages_spilt_by_punctuation.append(current_segment)
                current_segment = ""
        if current_segment:
            messages_spilt_by_punctuation.append(current_segment)

        final_messages.extend(messages_spilt_by_punctuation)
    return final_messages


async def _send_message(message: str):
    """
    发送消息给用户
    """
    # 移除 agent 指令会导致 4 个同时出现的换行符，要么替换为 2 个，要么提示用户
    message = message.strip().replace("\n\n\n\n", "\n\n")
    messages = _split_message(message)
    for msg in messages:
        await UniMessage(msg).send(target=_message_target, bot=get_bot())
        await asyncio.sleep(DELAYED_SECOND_PER_PARAGRAPH)


@at_event.handle()
async def handle_supported_adapters(
    bot_message: UniMsg,
    event: Event,
    bot: Bot,
    matcher: Matcher,
    target: MsgTarget,
    ext: ReplyRecordExtension,
    ipc_client: IpcClient = Depends(_get_ipc_client),
) -> None:
    """Main message handler -- receives user messages and forwards to Core."""
    if any((bot_message.startswith("."), bot_message.startswith("/"))):
        await UniMessage("未知的指令或权限不足").finish()

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

    userid = event.get_user_id()
    if not target.private:
        session = extract_session(bot, event)
        group_id = session.get_id(SessionIdType.GROUP)
    else:
        group_id = "-1"

    logger.info(f"Received message: {message_text} multimodal: {message_resource}")

    if not any((message_text, message_resource)):
        return

    await ipc_client.send_event(
        "user_message",
        {
            "message": {
                "message": message_text,
                "userid": userid,
                "groupid": group_id,
            },
            "resources": [r.to_dict() for r in message_resource],
        },
    )


@command_model.assign("help")
async def handle_model_help() -> None:
    await UniMessage(
        "Model 命令指南:\n"
        "  - help: 显示此帮助信息\n"
        "  - load <config_name>: 加载模型配置\n"
        "  - reload: 重新加载模型配置文件\n"
        "  - list: 列出所有可用的模型配置"
    ).finish()


@command_model.assign("reload")
async def handle_model_reload(ipc_client: IpcClient = Depends(_get_ipc_client)) -> None:
    await ipc_client.send_config_changed()
    await UniMessage("[System] 已发送重载请求").finish()


@command_model.assign("load")
async def handle_model_load(
    config: Match[str] = AlconnaMatch("config_name"), ipc_client: IpcClient = Depends(_get_ipc_client)
) -> None:
    config_name = config.result if config.available else None
    await ipc_client.send_config_changed(config_name)
    await UniMessage("[System] 已发送模型配置变更请求").finish()


@command_model.assign("list")
async def handle_model_list() -> None:
    config_manager = get_model_config_manager()
    configs = config_manager.configs
    outputs = ["目前所有可用的模型配置列表:"]
    for name, config in configs.items():
        outputs.append(f"-{name} {config.model_name}({config.provider}) 多模态: {'是' if config.multimodal else '否'}")
    await UniMessage("\n".join(outputs)).finish()


def _format_state_display(state: dict) -> str:
    """Format a state dict as human-readable display text."""
    at = state.get("active_topic")
    lines = [
        "当前 Muika 情绪状态:",
        f"  mood        : {state.get('mood', '?')}",
        f"  attention   : {state.get('attention', 0):.2f}",
        f"  loneliness  : {state.get('loneliness', 0):.2f}",
        f"  boredom     : {state.get('boredom', 0):.2f}",
        f"  curiosity   : {state.get('curiosity', 0):.2f}",
    ]
    if at:
        lines += [
            "活跃话题:",
            f"  topic_id    : {at.get('topic_id', '?')}",
            f"  topic_type  : {at.get('topic_type', '?')}",
            f"  topic_seed  : {at.get('topic_seed', '?')}",
            f"  user_engaged: {'是' if at.get('user_engaged') else '否'}",
        ]
    else:
        lines.append("活跃话题: 无")
    return "\n".join(lines)


@command_debug.assign("topic")
async def handle_debug_topic(ipc_client: IpcClient = Depends(_get_ipc_client)) -> None:
    await ipc_client.send_debug("trigger_topic")
    await UniMessage("已发送话题触发请求到 Core").finish()


@command_debug.assign("state")
async def handle_debug_state(ipc_client: IpcClient = Depends(_get_ipc_client)) -> None:
    await ipc_client.send_query("state")


@command_debug.assign("state-set")
async def handle_debug_state_set(
    field: Match[str] = AlconnaMatch("field"),
    value: Match[str] = AlconnaMatch("value"),
    ipc_client: IpcClient = Depends(_get_ipc_client),
) -> None:
    _FLOAT_FIELDS = {"attention", "loneliness", "boredom", "curiosity"}
    _STR_FIELDS = {"mood"}
    _ALL_FIELDS = _FLOAT_FIELDS | _STR_FIELDS

    field_name = field.result
    raw_value = value.result

    if field_name not in _ALL_FIELDS:
        await UniMessage(f"未知字段 '{field_name}'，可修改的字段: {', '.join(sorted(_ALL_FIELDS))}").finish()

    val = float(raw_value) if field_name in _FLOAT_FIELDS else raw_value
    await ipc_client.send_debug("set_state", field=field_name, value=val)
    await UniMessage(f"已发送状态修改请求: {field_name} = {val}").finish()


@command_debug.assign("topic-reset")
async def handle_debug_topic_reset(ipc_client: IpcClient = Depends(_get_ipc_client)) -> None:
    await ipc_client.send_debug("reset_topic")
    await UniMessage("已发送话题重置请求到 Core").finish()
