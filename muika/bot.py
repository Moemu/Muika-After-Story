import asyncio
import os
import re
import time
from pathlib import Path
from typing import AsyncGenerator, Literal
from urllib.parse import urlparse

from arclet.alconna import Alconna, AllParam, Args
from nonebot import get_driver, logger
from nonebot.adapters import Bot, Event
from nonebot.adapters import Message as BotMessage
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.rule import to_me
from nonebot_plugin_alconna import (
    MsgTarget,
    UniMessage,
    UniMsg,
    get_message_id,
    on_alconna,
    uniseg,
)
from nonebot_plugin_alconna.builtins.extensions import ReplyRecordExtension
from nonebot_plugin_session import SessionIdType, extract_session

from .config import load_embedding_model_config
from .core import UserMessagePayload, muika
from .core.events import UserMessageEvent
from .llm import ModelCompletions, ModelStreamCompletions
from .models import Message, Resource
from .plugin import load_plugins
from .plugin.mcp import initialize_servers
from .utils.SessionManager import SessionManager
from .utils.utils import download_file, get_file_via_adapter

COMMAND_PREFIXES = [".", "/"]
PLUGINS_PATH = Path("./plugins")
MCP_CONFIG_PATH = Path("./configs/mcp.json")
START_TIME = time.time()

connect_time = 0.0
driver = get_driver()
session_manager = SessionManager()


def startup_plugins():
    load_embedding_model_config()

    if PLUGINS_PATH.exists():
        logger.info("加载外部插件...")
        load_plugins("./plugins")

    # if mas_config.enable_builtin_plugins:
    #     logger.info("加载 MAS 内嵌插件...")
    #     builtin_plugins_path = Path(__file__).parent / "builtin_plugins"
    #     muicebot_plugins_path = Path(__file__).resolve().parent.parent
    #     load_plugins(builtin_plugins_path, base_path=muicebot_plugins_path)


# 启动时唤醒 Muika
@driver.on_startup
async def startup():
    logger.info("加载 MAS 框架...")
    logger.info("初始化 Muika 实例...")
    asyncio.create_task(muika.start())

    logger.info("加载 MAS 插件...")
    startup_plugins()

    if MCP_CONFIG_PATH.exists():
        logger.info("加载 MCP Server 配置")
        await initialize_servers()

    logger.success("插件加载完成")

    logger.success("MAS 主框架已准备就绪✨")


@driver.on_bot_connect
async def bot_connected():
    logger.success("Bot 已连接，消息处理进程开始运行✨")
    global connect_time
    if not connect_time:
        connect_time = time.time()


at_event = on_alconna(
    Alconna(re.compile(".+"), Args["text?", AllParam], separators=""),
    priority=100,
    rule=to_me(),
    block=True,
    extensions=[ReplyRecordExtension()],
)


def _get_media_filename(media: uniseg.segment.Media, type: Literal["audio", "image", "video", "file"]) -> str:
    """
    给多模态文件分配一个独一无二的文件名
    """
    _default_suffix = {"audio": "mp3", "image": "png", "video": "mp4", "file": ""}

    assert media.url  # 只能在 url 不为空时使用

    if media.name:
        file_suffix = media.name.split(".")[-1] if media.name.count(".") else _default_suffix[type]
    else:
        path = urlparse(media.url).path
        _, ext = os.path.splitext(path)
        file_suffix = ext.lstrip(".") if ext else _default_suffix[type]

    file_name = f"{time.time_ns()}.{file_suffix}"

    return file_name


async def _extract_multi_resource(
    message: UniMessage, type: Literal["audio", "image", "video", "file"], event: Event
) -> list[Resource]:
    """
    提取单个多模态文件
    """
    resources = []

    for resource in message:
        assert isinstance(resource, uniseg.segment.Media)  # 正常情况下应该都是 Media 的子类

        try:
            if resource.path is not None:
                path = str(resource.path)
            elif resource.url is not None:
                path = await download_file(resource.url, file_name=_get_media_filename(resource, type))
            elif resource.origin is not None:
                logger.warning("无法通过通用方式获取文件URL，回退至适配器自有方式...")
                path = await get_file_via_adapter(resource.origin, event)  # type:ignore
            else:
                continue

            if path:
                resources.append(Resource(type, path=path))
        except Exception as e:
            logger.error(f"处理文件失败: {e}")

    return resources


async def _extract_multi_resources(message: UniMsg, event: Event) -> list[Resource]:
    """
    提取多个多模态文件
    """
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


async def _send_multi_messages(resource: Resource):
    """
    发送多模态文件

    TODO: 我们有可能对发送对象添加文件名吗？
    """
    if resource.type == "audio":
        await UniMessage(uniseg.Voice(raw=resource.raw, path=resource.path)).send()
    elif resource.type == "image":
        await UniMessage(uniseg.Image(raw=resource.raw, path=resource.path)).send()
    elif resource.type == "video":
        await UniMessage(uniseg.Video(raw=resource.raw, path=resource.path)).send()
    else:
        await UniMessage(uniseg.File(raw=resource.raw, path=resource.path)).send()


async def _send_message(completions: ModelCompletions | AsyncGenerator[ModelStreamCompletions, None]):
    # non-stream
    if isinstance(completions, ModelCompletions):
        paragraphs = completions.text.split("\n\n")

        for index, paragraph in enumerate(paragraphs):
            if not paragraph.strip():
                continue  # 跳过空白文段
            if index == len(paragraphs) - 1:
                await UniMessage(paragraph).send()
                break
            await UniMessage(paragraph).send()

        if completions.resources:
            for resource in completions.resources:
                await _send_multi_messages(resource)

        raise FinishedException

    # stream
    current_paragraph = ""

    async for chunk in completions:
        logger.debug(chunk)
        current_paragraph += chunk.chunk
        paragraphs = current_paragraph.split("\n\n")

        while len(paragraphs) > 1:
            current_paragraph = paragraphs[0].strip()
            if current_paragraph:
                await UniMessage(current_paragraph).send()
            paragraphs = paragraphs[1:]

        current_paragraph = paragraphs[-1].strip()

        if chunk.resources:
            for resource in chunk.resources:
                await _send_multi_messages(resource)

    if current_paragraph:
        await UniMessage(current_paragraph).finish()


@at_event.handle()
async def handle_supported_adapters(
    bot_message: UniMsg,
    event: Event,
    bot: Bot,
    matcher: Matcher,
    target: MsgTarget,
    ext: ReplyRecordExtension,
):
    if any((bot_message.startswith("."), bot_message.startswith("/"))):
        await UniMessage("未知的指令或权限不足").finish()

    # 先拿到引用消息并合并到 message (如果有)
    if message_reply := ext.get_reply(get_message_id(event, bot)):
        reply_message = message_reply.msg
        if isinstance(reply_message, BotMessage):
            bot_message += UniMessage("\n被引用的消息: ") + UniMessage(reply_message)
        else:
            bot_message += UniMessage(f"\n被引用的消息: {reply_message}")

    # 然后等待新消息插入
    if not (merged_message := await session_manager.put_and_wait(event, bot_message)):
        matcher.skip()
        return  # 防止类型检查器错误推断 merged_message 类型)

    message_text = merged_message.extract_plain_text()
    message_resource = await _extract_multi_resources(merged_message, event)

    userid = event.get_user_id()
    if not target.private:
        session = extract_session(bot, event)
        group_id = session.get_id(SessionIdType.GROUP)
    else:
        group_id = "-1"

    logger.info(f"收到消息文本: {message_text} 多模态消息: {message_resource}")

    if not any((message_text, message_resource)):
        return

    message = Message(message=message_text, userid=userid, groupid=group_id, resources=message_resource)

    await muika.create_event(UserMessageEvent(UserMessagePayload(message)))
