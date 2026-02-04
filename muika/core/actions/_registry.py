from __future__ import annotations

from inspect import Parameter, signature
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Optional, cast

ActionHandler = Callable[..., Awaitable[str]]


_registry: Dict[str, ActionHandler] = {}


def register_action(
    name: str,
) -> Callable[[Callable[..., Awaitable[str]]], Callable[..., Awaitable[str]]]:
    def decorator(handler: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        if name in _registry:
            raise ValueError(f"Action '{name}' is already registered")
        _registry[name] = cast(ActionHandler, handler)
        return handler

    return decorator


def get_action_handler(name: str) -> Optional[ActionHandler]:
    return _registry.get(name)


def list_action_names() -> list[str]:
    return sorted(_registry.keys())


async def invoke_action(handler: ActionHandler, intent: Any, state: Any, executor: "Executor") -> str:
    sig = signature(handler)
    available = {
        "intent": intent,
        "state": state,
        "executor": executor,
    }

    kwargs: dict[str, object] = {}
    has_var_kw = False
    for name, param in sig.parameters.items():
        if param.kind is Parameter.VAR_KEYWORD:
            has_var_kw = True
            continue
        if param.kind is Parameter.VAR_POSITIONAL:
            raise TypeError("Action handler should not use *args parameters")

        if name in available:
            kwargs[name] = available[name]
            continue

        annotation = param.annotation
        if isinstance(annotation, str):
            if annotation in {"Executor", "MuikaState"}:
                kwargs[name] = available["executor" if annotation == "Executor" else "state"]
                continue
            if annotation == "Intent":
                kwargs[name] = available["intent"]
                continue
        elif annotation is not Parameter.empty:
            # 处理普通类型
            if isinstance(annotation, type):
                # 检查 MuikaState
                try:
                    from ..state import MuikaState

                    if annotation is MuikaState:
                        kwargs[name] = available["state"]
                        continue
                except (ImportError, AttributeError):
                    pass

                # 检查 Executor
                if hasattr(annotation, "__name__") and annotation.__name__ == "Executor":
                    kwargs[name] = available["executor"]
                    continue

                # 检查具体的 Intent 子类（如 SendMessageIntent）
                try:
                    from ..intents import IntentBase

                    if issubclass(annotation, IntentBase):
                        kwargs[name] = available["intent"]
                        continue
                except (ImportError, AttributeError, TypeError):
                    pass

        if param.default is Parameter.empty:
            raise TypeError(f"Cannot resolve parameter '{name}' for handler '{handler.__name__}'")

    if has_var_kw:
        for name, value in available.items():
            kwargs.setdefault(name, value)

    return await handler(**kwargs)


if TYPE_CHECKING:
    from ..executor import Executor
