import inspect
from collections import defaultdict
from contextvars import ContextVar
from typing import (
    Any,
    Awaitable,
    Dict,
    List,
    Optional,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.internal.matcher import current_bot, current_event, current_matcher
from nonebot.matcher import Matcher
from nonebot.rule import Rule
from nonebot.typing import T_State

from ._types import HOOK_ARGS, HOOK_FUNC, HookType

DEPENDENCY_PROVIDERS: dict[type, ContextVar] = {
    Bot: current_bot,
    Event: current_event,
    Matcher: current_matcher,
    # T_State: get_state,
}


def _match_union(param_type: type, arg: object) -> bool:
    if get_origin(param_type) is Union:
        return any(isinstance(arg, t) for t in get_args(param_type))
    return False


class HookManager:
    def __init__(self):
        self._hooks: Dict[HookType, List["Hooked"]] = defaultdict(list)

    async def _inject_dependencies(self, function: HOOK_FUNC, *hook_args: HOOK_ARGS) -> dict:
        """
        自动解析参数并进行依赖注入
        """
        sig = inspect.signature(function)
        hints = get_type_hints(function)

        inject_args: dict[str, Any] = {}

        for name, param in sig.parameters.items():
            param_type = hints.get(name, None)
            if not param_type:
                continue

            for hook_arg in hook_args:
                # 1. Union 类型注入 hook_arg
                if _match_union(param_type, hook_arg):
                    inject_args[name] = hook_arg
                    break

                # 2. 直接匹配 hook_arg
                if isinstance(hook_arg, param_type):
                    inject_args[name] = hook_arg
                    break

                # 3. 依赖提供者匹配（Bot、Event、Matcher...）
                for dep_type, provider in DEPENDENCY_PROVIDERS.items():
                    if isinstance(param_type, type) and issubclass(param_type, dep_type):
                        inject_args[name] = provider.get()
                        break

        return inject_args

    def register(self, hook_type: HookType, hooked: "Hooked"):
        """
        注册一个挂钩函数
        """
        self._hooks[hook_type].append(hooked)
        return hooked

    async def run(self, hook_type: HookType, *hook_args: HOOK_ARGS, stream: bool = False):
        """
        运行所有的钩子函数

        :param hook_type: 钩子类型
        :param hook_arg: 消息处理流程中对应的数据类
        :param stream: 当前是否为流式状态
        """
        hookeds = self._hooks[hook_type]
        hookeds.sort(key=lambda x: x.priority)

        bot: Bot = current_bot.get()
        event: Event = current_event.get()
        state: T_State = {}

        for hooked in hookeds:
            args = await self._inject_dependencies(hooked.function, *hook_args)

            if (hooked.stream is not None and hooked.stream == stream) or (
                hooked.rule and not await hooked.rule(bot, event, state)
            ):
                continue

            result = hooked.function(**args)
            if isinstance(result, Awaitable):
                await result


hook_manager = HookManager()


class Hooked:
    """挂钩函数对象"""

    def __init__(
        self, hook_type: HookType, priority: int = 10, stream: Optional[bool] = None, rule: Optional[Rule] = None
    ):
        self.hook_type = hook_type
        """钩子函数类型"""
        self.priority = priority
        """函数调用优先级"""
        self.stream = stream
        """是否仅在(非)流式中运行"""
        self.rule: Optional[Rule] = rule
        """启用规则"""

        self.function: HOOK_FUNC
        """函数对象"""

    def __call__(self, func: HOOK_FUNC) -> HOOK_FUNC:
        """
        修饰器：注册一个 Hook 函数
        """
        self.function = func

        # 获取模块名
        if module := inspect.getmodule(func):
            module_name = module.__name__.split(".")[-1]
        else:
            module_name = ""

        hook_manager.register(self.hook_type, self)
        logger.success(f"挂钩函数 {module_name}.{func.__name__} 已成功加载")
        return func


def on_before_pretreatment(priority: int = 10, rule: Optional[Rule] = None) -> Hooked:
    """
    注册一个钩子函数
    这个函数将在传入消息 (`Muice` 的 `_prepare_prompt()`) 前调用
    它可接受一个 `Message` 类参数

    :param priority: 调用优先级
    :param rule: Nonebot 的响应规则
    """
    return Hooked(HookType.BEFORE_PRETREATMENT, priority=priority, rule=rule)


def on_before_completion(priority: int = 10, rule: Optional[Rule] = None) -> Hooked:
    """
    注册一个钩子函数。
    这个函数将在传入模型(`Muice` 的 `model.ask()`)前调用
    它可接受一个 `ModelRequest` 类参数

    :param priority: 调用优先级
    :param rule: Nonebot 的响应规则
    """
    return Hooked(HookType.BEFORE_MODEL_COMPLETION, priority=priority, rule=rule)


def on_stream_chunk(priority: int = 10, rule: Optional[Rule] = None) -> Hooked:
    """
    注册一个钩子函数。
    这个函数将在流式调用中途(`Muice` 的 `model.ask()`)调用
    它可接受一个 `ModelStreamCompletions` 类参数

    :param priority: 调用优先级
    :param rule: Nonebot 的响应规则
    """
    return Hooked(HookType.ON_STREAM_CHUNK, priority=priority, rule=rule)


def on_after_completion(priority: int = 10, stream: Optional[bool] = None, rule: Optional[Rule] = None) -> Hooked:
    """
    注册一个钩子函数。
    这个函数将在传入模型(`Muice` 的 `model.ask()`)后调用（流式则传入整合后的数据）
    它可接受一个 `ModelCompletion` 类参数

    请注意：当启用流式时，对 `ModelStreamCompletion` 的任何修改将不生效

    :param priority: 调用优先级
    :param stream: 是否仅在(非)流式中处理，None 则无限制
    :param rule: Nonebot 的响应规则
    """
    return Hooked(HookType.AFTER_MODEL_COMPLETION, priority=priority, stream=stream, rule=rule)


def on_finish_chat(priority: int = 10, rule: Optional[Rule] = None) -> Hooked:
    """
    注册一个钩子函数。
    这个函数将在结束对话(存库前)调用
    它可接受一个 `Message` 类参数

    :param priority: 调用优先级
    :param rule: Nonebot 的响应规则
    """
    return Hooked(HookType.ON_FINISHING_CHAT, priority=priority, rule=rule)
