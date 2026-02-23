from __future__ import annotations

from inspect import Parameter, signature
from typing import TYPE_CHECKING, Any, Optional

from .types import ActionHandler, ActionOutput


async def invoke_registered(
    handler: ActionHandler,
    instance: Any,
    state: Any,
    executor: Optional["Executor"],
    instance_type: str,
) -> ActionOutput:
    sig = signature(handler)
    available = {
        instance_type: instance,
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

        if name in available and available[name] is not None:
            kwargs[name] = available[name]
            continue

        annotation = param.annotation
        if isinstance(annotation, str):
            if annotation in {"Executor", "MuikaState"}:
                kwargs[name] = available["executor" if annotation == "Executor" else "state"]
                continue
            if annotation in {"Intent", "Tool"}:
                kwargs[name] = available[instance_type]
                continue
        elif annotation is not Parameter.empty:
            if isinstance(annotation, type):
                try:
                    from ..state import MuikaState

                    if annotation is MuikaState:
                        kwargs[name] = available["state"]
                        continue
                except (ImportError, AttributeError):
                    pass

                if hasattr(annotation, "__name__") and annotation.__name__ == "Executor":
                    kwargs[name] = available["executor"]
                    continue

                try:
                    from ..perception.tools import BaseTool
                    from ..trigger.intents import BaseIntent

                    if issubclass(annotation, BaseIntent) or issubclass(annotation, BaseTool):
                        kwargs[name] = available[instance_type]
                        continue
                except (ImportError, AttributeError, TypeError):
                    pass

        if param.default is Parameter.empty and name not in kwargs:
            raise TypeError(f"Cannot resolve parameter '{name}' for handler '{handler.__name__}'")

    if has_var_kw:
        for name, value in available.items():
            if value is not None:
                kwargs.setdefault(name, value)

    result = await handler(**kwargs)

    if isinstance(result, str):
        return ActionOutput(content=result)
    if isinstance(result, ActionOutput):
        return result
    return ActionOutput(content=str(result))


if TYPE_CHECKING:
    from ..executor import Executor
