import os
import re
import time
from pathlib import Path
from typing import AsyncGenerator, Literal
from urllib.parse import urlparse

from arclet.alconna import Alconna, AllParam, Args
from nonebot import get_driver
from nonebot.adapters import Bot, Event
from nonebot.adapters import Message as BotMessage
from nonebot.exception import FinishedException
from nonebot.matcher import Matcher
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me
from nonebot_plugin_alconna import (
    AlconnaMatch,
    CommandMeta,
    Match,
    MsgTarget,
    Subcommand,
    UniMessage,
    UniMsg,
    get_message_id,
    on_alconna,
    uniseg,
)
from nonebot_plugin_alconna.builtins.extensions import ReplyRecordExtension
from nonebot_plugin_session import SessionIdType, extract_session

from muika.utils.logger import logger

from .config import get_model_config_manager
from .core import SessionBootstrapEvent, UserMessagePayload, muika
from .core.events import UserMessageEvent
from .llm import ModelCompletions, ModelStreamCompletions
from .models import Message, Resource
from .plugin import load_plugins
from .plugin.mcp import initialize_servers
from .utils.first_run import user_agreement
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
    user_agreement.check_first_run()
    logger.info("初始化 Muika 实例...")
    muika.start()

    logger.info("加载 MAS 插件...")
    startup_plugins()

    if MCP_CONFIG_PATH.exists():
        logger.info("加载 MCP Server 配置")
        await initialize_servers()

    logger.success("插件加载完成")

    logger.success("MAS 主框架已准备就绪✨")


@driver.on_bot_connect
async def bot_connected():
    logger.success("Bot 已连接")
    global connect_time
    if not connect_time:
        connect_time = time.time()
    await muika.memory.load()  # 先加载 DB，确保 new_session() 能正确判断 is_first_session
    muika.memory.new_session()
    await muika.create_event(SessionBootstrapEvent())
    logger.info("[Bootstrap] Session bootstrap event queued.")


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
        Subcommand("load", Args["config_name?", str], help_text="切换指定模型配置，用法: .model load <config_name> "),
        Subcommand("reload", help_text="重新加载模型配置文件"),
        Subcommand("list", help_text="列出所有可用模型配置"),
        meta=CommandMeta("Muicebot 模型配置管理指令"),
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
        Subcommand("topic", help_text="立即触发一次话题旁路管线，测试主动对话效果"),
        Subcommand("state", help_text="显示当前 Muika 情绪状态"),
        Subcommand(
            "state-set",
            Args["field", str]["value", str],
            help_text="修改情绪状态字段，用法: .debug state-set <field> <value>",
        ),
        Subcommand("topic-reset", help_text="清空当前活跃话题（active_topic）"),
        meta=CommandMeta("Muika 调试指令"),
    ),
    priority=10,
    block=True,
    skip_for_unmatch=False,
    permission=SUPERUSER,
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


@command_model.assign("help")
async def handle_model_help():
    await UniMessage(
        """Model 命令指南:
    - help: 显示此帮助信息
    - load <config_name>: 加载模型配置
    - reload: 重新加载模型配置文件
    - list: 列出所有可用的模型配置
    """
    ).finish()


@command_model.assign("reload")
async def handle_model_reload():
    logger.info("重新加载模型配置文件...")
    config_manager = get_model_config_manager()

    try:
        config_manager._on_config_changed()
    except Exception as e:
        await UniMessage(str(e)).finish()

    await UniMessage(f"已成功重载模型配置文件: {config_manager.current_config}").finish()


@command_model.assign("load")
async def handle_model_load(config: Match[str] = AlconnaMatch("config_name")):
    config_manager = get_model_config_manager()
    config_name = config.result if config.available else None

    try:
        new_config = config_manager.get_model_config(config_name)
        config_manager.change_current_config(new_config)
    except (ValueError, FileNotFoundError) as e:
        await UniMessage(str(e)).finish()

    await UniMessage(
        f"已成功加载 {config_name}"
        if config_name
        else f"未指定模型配置名，已加载默认模型配置: {config_manager.current_config}"
    ).finish()


@command_model.assign("list")
async def handle_model_list():
    config_manager = get_model_config_manager()
    configs = config_manager.configs
    outputs = ["目前所有可用的模型配置列表:"]

    for name, config in configs.items():
        outputs.append(f"-{name} {config.model_name}({config.provider}) 多模态: {'是' if config.multimodal else '否'}")

    await UniMessage("\n".join(outputs)).finish()


@command_debug.assign("topic")
async def handle_debug_topic():
    await UniMessage("Debug: 正在触发话题管线...").send()
    await muika._run_topic_pipeline()


@command_debug.assign("state")
async def handle_debug_state():
    s = muika.state
    at = s.active_topic

    lines = [
        "当前 Muika 情绪状态:",
        f"  mood        : {s.mood}",
        f"  attention   : {s.attention:.2f}",
        f"  loneliness  : {s.loneliness:.2f}",
        f"  boredom     : {s.boredom:.2f}",
        f"  curiosity   : {s.curiosity:.2f}",
    ]

    if at is not None:
        lines += [
            "活跃话题:",
            f"  topic_id    : {at.topic_id}",
            f"  topic_type  : {at.topic_type}",
            f"  topic_seed  : {at.topic_seed}",
            f"  started_at  : {at.started_at.strftime('%H:%M:%S')}",
            f"  user_engaged: {'是' if at.user_engaged else '否'}",
        ]
    else:
        lines.append("活跃话题: 无")

    await UniMessage("\n".join(lines)).finish()


@command_debug.assign("state-set")
async def handle_debug_state_set(
    field: Match[str] = AlconnaMatch("field"),
    value: Match[str] = AlconnaMatch("value"),
):
    _FLOAT_FIELDS = {"attention", "loneliness", "boredom", "curiosity"}
    _STR_FIELDS = {"mood"}
    _ALL_FIELDS = _FLOAT_FIELDS | _STR_FIELDS

    field_name = field.result
    raw_value = value.result

    if field_name not in _ALL_FIELDS:
        await UniMessage(f"未知字段 '{field_name}'，可修改的字段: {', '.join(sorted(_ALL_FIELDS))}").finish()

    if field_name in _FLOAT_FIELDS:
        try:
            float_val = float(raw_value)
        except ValueError:
            await UniMessage(f"字段 '{field_name}' 需要一个浮点数值（0.0 ~ 1.0）").finish()
        if not (0.0 <= float_val <= 1.0):
            await UniMessage(f"字段 '{field_name}' 的值须在 0.0 ~ 1.0 范围内").finish()
        setattr(muika.state, field_name, float_val)
        await UniMessage(f"已将 {field_name} 设置为 {float_val:.2f}").finish()
    else:
        setattr(muika.state, field_name, raw_value)
        await UniMessage(f"已将 {field_name} 设置为 '{raw_value}'").finish()


@command_debug.assign("topic-reset")
async def handle_debug_topic_reset():
    if muika.state.active_topic is None:
        await UniMessage("当前没有活跃话题，无需重置。").finish()
    topic_id = muika.state.active_topic.topic_id
    muika.state.active_topic = None
    await UniMessage(f"已清空活跃话题: {topic_id}").finish()
