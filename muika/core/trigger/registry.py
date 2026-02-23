from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Optional, cast

from ..actions import ActionHandler, ActionOutput, invoke_registered

_intent_registry: Dict[str, ActionHandler] = {}


def register_intent(name: str):
    def decorator(handler: ActionHandler) -> ActionHandler:
        if name in _intent_registry:
            raise ValueError(f"Intent '{name}' is already registered")
        _intent_registry[name] = cast(ActionHandler, handler)
        return handler

    return decorator


def get_intent_handler(name: str) -> Optional[ActionHandler]:
    return _intent_registry.get(name)


def list_intent_names() -> list[str]:
    return sorted(_intent_registry.keys())


async def invoke_intent(handler: ActionHandler, intent: Any, state: Any, executor: "Executor") -> ActionOutput:
    return await invoke_registered(handler, intent, state, executor, "intent")


if TYPE_CHECKING:
    from ..executor import Executor
