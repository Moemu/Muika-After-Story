"""Core process entry point.

Usage::

    python -m muika.ipc.bootstrap [--host 127.0.0.1] [--port 8765]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from muika.config import get_model_config_manager, mas_config
from muika.core.events import (
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.core.state import MuikaState
from muika.database.crud import UsageORM
from muika.database.db import close_db, get_session, init_db
from muika.models import Message
from muika.plugin.mcp import initialize_servers
from muika.utils.logger import init_logger, logger
from muika.utils.utils import get_version

from .protocol import ActionResponse, ErrorMessage, QueryResponse, SendMessage
from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer

MCP_CONFIG_PATH = Path("./configs/mcp.json")


class CoreBootstrap:
    """Wires up the WS server, Executor, and Muika engine.

    :param host: WebSocket listen address.
    :param port: WebSocket listen port.
    :param ipc_secret: IPC 预共享密钥。
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        ipc_secret: str = mas_config.ipc_secret,
    ) -> None:
        self._host = host
        self._port = port

        self._ws_server = CoreWsServer(host=host, port=port, secret=ipc_secret)

        async def send_func(content: str) -> None:
            """Route messages from Muika to the Bot via WebSocket."""
            msg = SendMessage(content=content)
            ok = await self._ws_server.send_to_bot(msg)
            if not ok:
                logger.warning("[Core] Message dropped, no Bot connected")

        event_queue: asyncio.Queue = asyncio.Queue()
        self._executor = Executor(event_queue, send_func=send_func)
        self._muika = Muika(self._executor)

        self._shutdown_event = asyncio.Event()

        self.is_bootstraped = False

    async def start(self) -> None:
        """Boot all components."""
        logger.info("Muika Core is booting...")

        self._register_handlers()
        await self._ws_server.start()

        self._muika.start()

        logger.success(
            f"Muika Core is ready -- ws://{self._host}:{self._port}/ws "
            f"(health: http://{self._host}:{self._port}/health)"
        )

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        if self._shutdown_event.is_set():
            return

        logger.info("Muika Core is shutting down...")

        self._shutdown_event.set()
        self._muika.stop()
        await self._ws_server.stop()
        await close_db()

        logger.success("Muika Core stopped")

    def _register_handlers(self) -> None:
        """Register WS message handlers."""
        self._ws_server.register_handler("event", self._handle_event)
        self._ws_server.register_handler("query", self._handle_query)
        self._ws_server.register_handler("debug", self._handle_debug)
        self._ws_server.register_handler("config_changed", self._handle_config_changed)
        self._ws_server.register_handler("session", self._handle_session)

    async def _handle_event(self, message: dict) -> ActionResponse | ErrorMessage:
        """Forward a Bot event into the Muika event queue."""
        event_data = message.get("event", {})
        event_type = event_data.get("event_type", "unknown")
        payload = event_data.get("payload", {})

        logger.info(f"[Core] Received event: {event_type}")

        if event_type == "user_message":
            msg_data = payload.get("message", {})
            msg = Message(
                message=msg_data.get("message", ""),
                userid=msg_data.get("userid", ""),
                groupid=msg_data.get("groupid", "-1"),
            )
            await self._muika.create_event(UserMessageEvent(UserMessagePayload(msg)))
            return ActionResponse(action=event_type, status="queued")

        if event_type == "bot_connected" and not self.is_bootstraped:
            # 避免重复开始新对话
            if self.is_bootstraped:
                return ActionResponse(action=event_type, status="ok")
            self.is_bootstraped = True
            self._muika.memory.new_session()
            last_chat_str = payload.get("last_chat_time")
            last_chat = datetime.fromisoformat(last_chat_str) if last_chat_str else None
            await self._muika.create_event(SessionBootstrapEvent(last_chat_time=last_chat))
            return ActionResponse(action=event_type, status="queued")

        if event_type == "session_end":
            await self._muika.create_event(SessionEndEvent())
            return ActionResponse(action=event_type, status="queued")

        logger.debug(f"[Core] Unknown event type: {event_type}")
        return ErrorMessage(message="unknown_event_type")

    async def _handle_query(self, message: dict) -> QueryResponse | ErrorMessage:
        """Handle state queries from the Bot."""
        query_type = message.get("query", "state")
        if query_type == "state":
            state = deepcopy(self._muika.state)
            state.memory = None
            return QueryResponse(query="state", data=asdict(state))

        if query_type == "usage":
            async with get_session() as session:
                records = await UsageORM.get_usage_records(session, days=7)
            manager = get_model_config_manager()
            data_rows = []
            totals = {"input": 0, "output": 0, "cached": 0, "cost": 0.0}
            for r in records:
                row = {
                    "date": r.date,
                    "plugin": r.plugin,
                    "model": r.model or "",
                    "type": r.type,
                    "input_tokens": r.input_tokens or 0,
                    "output_tokens": r.output_tokens or 0,
                    "cached_tokens": r.cached_tokens or 0,
                }
                # 查找对应的 ModelConfig 并计算费用
                config = manager.configs.get(r.model) or manager.configs.get(r.plugin)
                if config and config.input_price is not None:
                    row["cost"] = round(
                        (
                            (r.input_tokens or 0) * config.input_price
                            + (r.output_tokens or 0) * (config.output_price or 0)
                            + (r.cached_tokens or 0) * (config.cached_price or 0)
                        )
                        / 1_000_000,
                        4,
                    )
                    totals["cost"] += row["cost"]
                data_rows.append(row)
                totals["input"] += r.input_tokens or 0
                totals["output"] += r.output_tokens or 0
                totals["cached"] += r.cached_tokens or 0
            return QueryResponse(query="usage", data={"records": data_rows, "totals": totals})

        return ErrorMessage(message="unknown_state")

    async def _handle_debug(self, message: dict) -> ActionResponse | ErrorMessage:
        """Handle debug commands from the Bot."""
        action = message.get("action", "")
        field = message.get("field")
        value = message.get("value")
        logger.info(f"[Core] Debug action: {action}")

        if action == "trigger_topic":
            await self._muika._run_topic_pipeline()
            return ActionResponse(action="trigger_topic", status="ok")

        if action == "set_state" and field and value is not None:
            _apply_state_field(self._muika.state, field, value)
            return ActionResponse(action="set_state", status="ok")

        if action == "reset_topic":
            self._muika.state.active_topic = None
            return ActionResponse(action="reset_topic", status="ok")

        return ErrorMessage(message="unknown_action")

    async def _handle_config_changed(self, message: dict) -> ActionResponse | ErrorMessage:
        """Handle model config change notification from the Bot."""
        config_name = message.get("config_name")
        logger.info(f"[Core] Config changed: {config_name}")
        try:
            manager = get_model_config_manager()
            manager._on_config_changed()
        except Exception as e:
            logger.warning(f"[Core] Failed to reload model: {e}")
            ErrorMessage(message="unknown error", detail=str(e))
        return ActionResponse(action="config_changed", status="ok")

    async def _handle_session(self, message: dict) -> ActionResponse | ErrorMessage:
        """Handle session management commands from the Bot."""
        action = message.get("action", "")
        logger.info(f"[Core] Session action: {action}")

        if action == "new_session":
            await self._muika._handle_session_end()
            return ActionResponse(action="new_session", status="ok")

        if action == "save_session":
            await self._muika._update_session_memory()
            return ActionResponse(action="save_session", status="ok")

        return ErrorMessage(message="unknown_session_action")


def _apply_state_field(s: MuikaState, field: str, value: Any) -> None:
    """Safely set a field on MuikaState."""
    float_fields = {"attention", "loneliness", "boredom", "curiosity"}
    str_fields = {"mood"}
    if field in float_fields:
        try:
            v = float(value)
            if 0.0 <= v <= 1.0:
                setattr(s, field, v)
        except (ValueError, TypeError):
            pass
    elif field in str_fields:
        setattr(s, field, str(value))


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Muika Core -- standalone AI companion backend")
    parser.add_argument(
        "--host",
        default=os.getenv("MUIKA_CORE_HOST", DEFAULT_HOST),
        help=f"WebSocket listen address (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MUIKA_CORE_PORT", str(DEFAULT_PORT))),
        help=f"WebSocket listen port (default: {DEFAULT_PORT})",
    )
    return parser.parse_args(argv)


async def run_core(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    """Async entry point for the Core process."""
    init_logger()

    logger.info(f"Muika-After-Story 版本: {get_version()}")
    logger.info(f"Muika-After-Story 数据目录: {mas_config.data_dir.resolve()}")

    logger.debug("Loading Database...")
    await init_db()

    if MCP_CONFIG_PATH.exists():
        logger.info("Loading MCP Server config")
        await initialize_servers()

    bootstrap = CoreBootstrap(host=host, port=port, ipc_secret=mas_config.ipc_secret)
    await bootstrap.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("[Core] Interrupted by user")

    await bootstrap.stop()


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point for the Core process."""
    args = _parse_args(argv)
    asyncio.run(run_core(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
