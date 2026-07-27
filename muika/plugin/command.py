"""命令注册与派发模块。

当 Bot 转发一条 command 事件到 Core 时，该模块负责：

1. 管理全局命令注册表（通过 :func:`on_alconna` 注册 :class:`CommandRegistry`）
2. 按优先级匹配 Alconna 解析结果，路由到对应的子命令处理器
3. 镜像 :mod:`muika.plugin.func_call` 的依赖注入风格，
   将 ``MuikaState`` / ``MuikaBrain`` / ``Muika`` 等核心实例以类型注解形式
   注入 handler 参数
4. 通过 :class:`CommandDispatcher` 单例管理注入表和回复通道，
   使 handler 可通过 :meth:`CommandRegistry.finish` 直接回复

用法示例::

    from arclet.alconna import Alconna, Option, Subcommand, Args
    from muika.plugin.command import on_alconna

    alc = Alconna("notes", Subcommand("add", Args["content", str], dest="add"), Option("list"))
    notes = on_alconna(alc, aliases={"note"})

    @notes.assign("add")
    async def cmd_add(content: str, state: MuikaState, memory: Memory):
        await memory.upsert_memory(...)
        await notes.finish(f"已保存: {content}")
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Coroutine,
    Optional,
    get_type_hints,
)

import aiofiles

from muika.config import mas_config
from muika.models import Resource
from muika.utils.logger import logger

if TYPE_CHECKING:
    from arclet.alconna import Alconna
    from arclet.alconna import Arparma as ArparmaType

    from muika.core.loop import Muika

COMMAND_PREFIXES: tuple[str, ...] = (".", "/")
"""命令前缀。Bot 侧匹配此前缀的消息统一以 command 事件转发给 Core。"""

_commands: list[CommandRegistry] = []
"""已注册的命令列表。"""

_MAX_RESOURCE_SIZE = 100 * 1024 * 1024  # 100 MiB
"""命令插件落盘资源的单文件最大字节数。"""


def _sanitize_extension(ext: str | None) -> str:
    """返回安全的文件扩展名（始终以 ``.`` 开头，仅保留字母数字）。"""
    if not ext:
        return ".bin"
    ext = ext.strip()
    if not ext.startswith("."):
        ext = f".{ext}"
    # 仅保留字母数字字符
    ext = "".join(c for c in ext if c.isalnum() or c == ".")
    return ext if len(ext) > 1 else ".bin"


async def _ensure_resource_path(resource: Resource) -> None:
    """若 Resource 仅有 raw 但无 path，落盘到 data_dir/downloads。

    :raises ValueError: raw 数据超过 ``_MAX_RESOURCE_SIZE``
    """
    if resource.path:
        return
    if not resource.raw:
        return

    data: bytes
    if isinstance(resource.raw, bytes):
        data = resource.raw
    else:
        data = resource.raw.getvalue()

    if len(data) > _MAX_RESOURCE_SIZE:
        raise ValueError(f"Resource size {len(data)} exceeds maximum {_MAX_RESOURCE_SIZE} bytes")

    tmp_dir = mas_config.data_dir / "downloads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    ext = _sanitize_extension(resource.extension)
    filepath = tmp_dir / f"{uuid.uuid4().hex}{ext}"
    async with aiofiles.open(filepath, "wb") as f:
        await f.write(data)
    resource.path = str(filepath.resolve())
    logger.debug(f"[Command] Persisted resource → {resource.path}")


class FinishedException(BaseException):
    """
    指示结束当前 Handler。
    """


@dataclass
class CommandRegistry:
    """单个命令的注册句柄。

    由 :func:`on_alconna` 创建并加入全局 ``_commands`` 列表。
    通过 ``assign(dest)`` / ``handle()`` 装饰器注册子命令处理器。
    """

    alc: Alconna
    """Alconna 解析器实例"""
    aliases: set[str] = field(default_factory=set)
    """命令别名。派发时将头部 token 替换为主命令名再解析"""
    priority: int = 10
    """越小越优先匹配"""

    _handlers: dict[str, Callable[..., Coroutine[None, None, None]]] = field(default_factory=dict)
    """dest → handler 映射。由 ``assign(dest)`` 装饰器填充"""

    _default_handler: Optional[Callable[..., Coroutine[None, None, None]]] = None
    """``handle()`` 装饰器注册的默认处理器（无子命令命中时执行）"""

    @property
    def handlers(self) -> dict[str, Callable[..., Coroutine[None, None, None]]]:
        """所有已注册的 dest → handler 映射（含默认处理器，以 ``__default__`` 标记）。"""
        result: dict[str, Callable[..., Coroutine[None, None, None]]] = {}
        if self._default_handler is not None:
            result["__default__"] = self._default_handler
        result.update(self._handlers)
        return result

    def assign(self, dest: str) -> Callable:
        """
        注册一个子命令/选项处理器。

        :param dest: 子命令名称

        Usage::

            @registry.assign("add")
            async def cmd_add(name: str): ...
        """

        def decorator(func):
            self._handlers[dest] = func
            return func

        return decorator

    def handle(self):
        """
        注册默认处理器（无子命令命中时执行）。

        Usage::

            @registry.handle()
            async def default_cmd(arparma: Arparma): ...
        """

        def decorator(func):
            self._default_handler = func
            return func

        return decorator

    async def finish(self, *message: str | Resource) -> None:
        """
        发送回复并终止命令处理。

        :param message: 要回复的内容。

        :raise FinishedException: 内部异常，由派发器的 try/except 捕获，对外不可见
        :raise RuntimeError: CommandDispatcher 尚未初始化
        """
        dispatcher = CommandDispatcher.get()
        text_parts: list[str] = []
        resource_dicts: list[dict] = []

        for item in message:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, Resource):
                await _ensure_resource_path(item)
                resource_dicts.append(item.to_dict())

        text = "\n".join(text_parts) if text_parts else ""
        resources = resource_dicts if resource_dicts else None
        await dispatcher._command_reply(text, resources)
        raise FinishedException()


def on_alconna(
    alc: Alconna,
    *,
    aliases: set[str] | None = None,
    priority: int = 10,
) -> CommandRegistry:
    """
    注册一个 Alconna 命令解析器并返回 :class:`CommandRegistry` 句柄。

    :param alc: Alconna 解析器实例
    :param use_command_start: 是否需要命令前缀。为 True 时派发器会从 raw 剥离前导 ``.`` 或 ``/``
    :param aliases: 命令别名（不含前缀）。命中别名时自动替换为主命令名再解析
    :param priority: 匹配优先级，越小越先尝试

    :return CommandRegistry: 注册句柄，可继续 ``.assign(dest)`` 或 ``.handle()`` 绑定处理器
    """
    registry = CommandRegistry(
        alc=alc,
        aliases=aliases or set(),
        priority=priority,
    )
    _commands.append(registry)
    logger.debug(f"[Command] Registered '{alc.command}' (priority={priority}, aliases={registry.aliases})")
    return registry


def get_commands() -> list[CommandRegistry]:
    """获取按优先级排序的命令注册表列表。"""
    return sorted(_commands, key=lambda c: c.priority)


class CommandDispatcher:
    """命令派发器单例。

    在 Core 启动时通过 :meth:`setup` 注入 :class:`Muika` 实例，
    此后所有命令通过 :meth:`dispatch` 派发，无需额外参数。
    """

    _instance: Optional[CommandDispatcher] = None

    def __init__(
        self, muika: Muika, command_reply: Callable[[str, Optional[list[dict]]], Coroutine[None, None, None]]
    ) -> None:
        from muika.core.brain import MuikaBrain
        from muika.core.butler.agent import ButlerAgent
        from muika.core.executor import Executor
        from muika.core.loop import Muika as MuikaCls
        from muika.core.memory import MemoryManager
        from muika.core.state import MuikaState
        from muika.core.topic_manager import TopicManager

        self.muika = muika
        self._command_reply = command_reply
        self._injections: dict[type, Any] = {
            MuikaCls: muika,
            MuikaState: muika.state,
            MuikaBrain: muika.brain,
            MemoryManager: muika.memory,
            Executor: muika.executor,
            TopicManager: muika.topic_manager,
            ButlerAgent: muika.butler_agent,
        }

    @classmethod
    def setup(
        cls,
        muika: Muika,
        command_reply: Callable[[str, Optional[list[dict]]], Coroutine[None, None, None]],
    ) -> CommandDispatcher:
        """初始化命令派发器。

        在 CoreBootstrap 构造后、Muika 实例可用时调用一次。
        """
        cls._instance = CommandDispatcher(muika, command_reply)
        logger.debug("[CommandDispatcher] Initialized")
        return cls._instance

    @classmethod
    def get(cls) -> CommandDispatcher:
        """获取派发器实例。"""
        if cls._instance is None:
            raise RuntimeError(
                "CommandDispatcher has not been initialized, "
                "call CommandDispatcher.setup(muika, command_reply) first"
            )
        return cls._instance

    async def dispatch(self, raw: str) -> None:
        """派发一条命令消息。

        依次按优先级尝试每个 :class:`CommandRegistry`，对匹配的 Alconna
        结果路由到对应的子命令处理器。
        未匹配任何命令时回复 "未知的指令"。
        """
        # 1. 剥离前缀
        cmd_raw = raw
        if raw and raw[0] in COMMAND_PREFIXES:
            cmd_raw = raw[1:].lstrip()

        if not cmd_raw:
            await self._command_reply("未知的指令", None)
            return

        # 2. 按优先级依序尝试
        for registry in get_commands():
            parse_text = cmd_raw

            # 别名替换
            head = parse_text.split(maxsplit=1)[0] if parse_text.strip() else ""
            if head and head in registry.aliases:
                root_name = registry.alc.command
                tail = parse_text[len(head) :]
                parse_text = root_name + tail
                logger.debug(f"[Command] Alias '{head}' → '{root_name}'; parsing '{parse_text}'")

            res = registry.alc.parse(parse_text)
            if not res.matched:
                continue

            logger.debug(f"[Command] Matched '{registry.alc.command}' ← '{cmd_raw}'")

            # 3. 路由到处理器
            matched = False
            for dest, handler in registry._handlers.items():
                if res.query(dest) is not None:
                    logger.debug(f"[Command] Routing to '{dest}' handler")
                    await self._invoke(handler, registry, res)
                    matched = True
                    break

            if not matched and registry._default_handler is not None:
                logger.debug("[Command] Routing to default handler")
                await self._invoke(registry._default_handler, registry, res)
                matched = True

            if matched:
                return

        # 无匹配
        await self._command_reply("未知的指令", None)

    async def _invoke(
        self,
        handler: Callable[..., Coroutine[None, None, None]],
        registry: CommandRegistry,
        res: ArparmaType,
    ) -> None:
        """构造依赖注入 kwargs 并执行 cmd handler。

        注入顺序（优先匹配优先）：
        1. 类型命中注入表中的核心实例
        2. 类型为 ``Arparma`` → 注入完整解析结果
        3. 参数名命中 ``res.all_matched_args`` → 注入解析值
        4. 参数有默认值 → 使用默认值
        5. 否则报 ``TypeError``

        执行后若 handler 返回非空 str → 自动调用 ``registry.finish(result)``。
        """
        from arclet.alconna import Arparma as Arparma

        sig = inspect.signature(handler)
        hints = get_type_hints(handler)
        kwargs: dict[str, Any] = {}

        for name, param in sig.parameters.items():
            t = hints.get(name)

            # 类型命中核心注入表
            if t is not None and t in self._injections:
                kwargs[name] = self._injections[t]
                continue

            # Arparma 完整解析结果
            if t is Arparma:
                kwargs[name] = res
                continue

            # 参数名命中 all_matched_args
            if name in res.all_matched_args:
                kwargs[name] = res.all_matched_args[name]
                continue

            # 默认值
            if param.default is not inspect.Parameter.empty:
                kwargs[name] = param.default
                continue

            raise TypeError(
                f"Cannot inject parameter '{name}: {t}' of handler "
                f"'{handler.__name__}' — not in all_matched_args and no default value. "
                f"Parsed args: {list(res.all_matched_args.keys())}"
            )

        try:
            result = await handler(**kwargs)  # type: ignore[func-returns-value]
            if isinstance(result, str):
                await registry.finish(result)
        except FinishedException:
            pass
