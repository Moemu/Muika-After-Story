from __future__ import annotations

from typing import Any, Dict, Optional, cast

from ..actions import ActionHandler, ActionOutput, invoke_registered

_tool_registry: Dict[str, ActionHandler] = {}


def register_tool(name: str):
    def decorator(handler: ActionHandler) -> ActionHandler:
        if name in _tool_registry:
            raise ValueError(f"Tool '{name}' is already registered")
        _tool_registry[name] = cast(ActionHandler, handler)
        return handler

    return decorator


def get_tool_handler(name: str) -> Optional[ActionHandler]:
    return _tool_registry.get(name)


def list_tool_names() -> list[str]:
    return sorted(_tool_registry.keys())


async def invoke_tool(handler: ActionHandler, tool: Any, state: Any) -> ActionOutput:
    return await invoke_registered(handler, tool, state, None, "tool")
