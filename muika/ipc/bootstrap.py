"""Core process entry point.

Usage::

    python -m muika.ipc.bootstrap [--host 127.0.0.1] [--port 8765]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from pathlib import Path
from typing import Optional

from pydantic import TypeAdapter

from muika.config import mas_config
from muika.core.events import (
    AdapterOfflineEvent,
    AdapterOnlineEvent,
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.core.self_mod.proposals import (
    core_maintenance_message,
    get_core_proposal_manager,
    is_core_maintenance_active,
    is_maintenance_command_allowed,
)
from muika.database.db import close_db, init_db
from muika.models import Message, Resource
from muika.plugin import CommandDispatcher, load_plugins
from muika.plugin.manager import get_plugin_manager
from muika.plugin.watcher import start_plugin_watcher, stop_plugin_watcher
from muika.template.loader import validate_template_configuration
from muika.utils.logger import init_logger, logger
from muika.utils.utils import get_version

from .protocol import ActionResponse, BotToCoreEvent, BotToCoreMessage
from .protocol import CommandEvent as IpcCommandEvent
from .protocol import CommandResult, ErrorMessage, SendMessage
from .protocol import SessionBootstrapEvent as IpcSessionBootstrapEvent
from .protocol import SessionEndEvent as IpcSessionEndEvent
from .protocol import UserMessageEvent as IpcUserMessageEvent
from .server import DEFAULT_HOST, DEFAULT_PORT, AdapterInfo, CoreWsServer

MCP_CONFIG_PATH = Path("./configs/mcp.json")
BUILTIN_PLUGINS_PATH = Path("muika/builtin_plugins")
"""内置插件目录。在 Core 启动时最早加载。"""


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

        async def _send_llm_reply(
            content: str, resources: list[Resource] | None = None, target: str | None = None
        ) -> None:
            """LLM 对话回复通过 SendMessage 发送，可按 *target* 路由到指定适配器。"""
            resources_dict = [r.to_dict() for r in resources] if resources else []
            msg = SendMessage(content=content, resources=resources_dict)
            ok = await self._ws_server.send_to_bot(msg, target=target)
            if not ok:
                logger.warning("[Core] LLM reply dropped, no Bot connected")

        async def _send_command_result(
            content: str, resources: list[dict] | None = None, target: str | None = None
        ) -> None:
            """命令执行结果通过 CommandResult 发送。"""
            msg = CommandResult(content=content, resources=resources or [])
            ok = await self._ws_server.send_to_bot(msg, target=target)
            if not ok:
                logger.warning("[Core] Command result dropped, no Bot connected")

        event_queue: asyncio.Queue = asyncio.Queue()
        self._executor = Executor(event_queue, send_func=_send_llm_reply)
        self._muika = Muika(self._executor)

        self._shutdown_event = asyncio.Event()
        self.is_bootstraped = False

        CommandDispatcher.setup(self._muika, _send_command_result)

    async def start(self) -> None:
        """Boot all components."""
        logger.info("Muika Core is booting...")

        validate_template_configuration((mas_config.persona_template, mas_config.agent_template))

        self._register_handlers()
        self._register_adapter_callbacks()
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
        stop_plugin_watcher()
        get_plugin_manager().shutdown_all()
        self._muika.stop()
        await self._ws_server.stop()
        await close_db()

        logger.success("Muika Core stopped")

    def _register_handlers(self) -> None:
        for msg_type in ("user_message", "command", "session_bootstrap", "session_end"):
            self._ws_server.register_handler(msg_type, self._handle_event)

    def _register_adapter_callbacks(self) -> None:
        """注册适配器连接 / 断开回调，将事件推入 Muika 事件队列。"""

        async def _on_adapter_connected(adapter: AdapterInfo) -> None:
            logger.info(f"[Core] Adapter online: {adapter!r}")
            if self.is_bootstraped:
                await self._muika.create_event(AdapterOnlineEvent(adapter=adapter))

        async def _on_adapter_disconnected(adapter: AdapterInfo) -> None:
            logger.info(f"[Core] Adapter offline: {adapter!r}")
            if self.is_bootstraped:
                await self._muika.create_event(AdapterOfflineEvent(adapter=adapter))

        self._ws_server.on_adapter_connected(_on_adapter_connected)
        self._ws_server.on_adapter_disconnected(_on_adapter_disconnected)

    async def _handle_event(self, message: dict, adapter: AdapterInfo) -> ActionResponse | ErrorMessage:
        """Forward a Bot event into the Muika event queue.

        :param message: 解析后的 JSON dict
        :param adapter: 来源适配器
        """
        event: BotToCoreEvent

        client_name = adapter.client_name
        try:
            event = TypeAdapter[BotToCoreEvent](BotToCoreMessage).validate_python(message)
        except Exception as e:
            logger.error(f"[Core] Failed to parse IPC event: {e}")
            return ErrorMessage(message="invalid_event", detail=str(e))

        logger.info(f"[Core] Received event: {event.type} from {client_name!r}")

        if is_core_maintenance_active():
            if isinstance(event, IpcCommandEvent) and is_maintenance_command_allowed(event.raw):
                pass
            elif isinstance(event, IpcUserMessageEvent):
                self._ws_server.set_triggering_adapter(client_name)
                await self._ws_server.send_to_bot(SendMessage(content=core_maintenance_message()), target=client_name)
                return ActionResponse(action=event.type, status="maintenance")
            elif isinstance(event, IpcCommandEvent):
                self._ws_server.set_triggering_adapter(client_name)
                await self._ws_server.send_to_bot(
                    CommandResult(content="[System] Core 正在等待重启。当前命令在维护模式中不可用。"),
                    target=client_name,
                )
                return ActionResponse(action=event.type, status="maintenance")
            else:
                if isinstance(event, IpcSessionBootstrapEvent):
                    self._ws_server.mark_bootstrapped(client_name)
                    self.is_bootstraped = True
                return ActionResponse(action=event.type, status="maintenance")

        if isinstance(event, IpcUserMessageEvent):
            self._ws_server.set_triggering_adapter(client_name)
            msg = Message(message=event.message)
            await self._muika.create_event(UserMessageEvent(UserMessagePayload(msg)))
            return ActionResponse(action=event.type, status="queued")

        if isinstance(event, IpcCommandEvent):
            self._ws_server.set_triggering_adapter(client_name)
            await CommandDispatcher.get().dispatch(event.raw)
            return ActionResponse(action=event.type, status="ok")

        if isinstance(event, IpcSessionBootstrapEvent):
            logger.info(f"[Core] Adapter {client_name!r} joined existing session")
            self._ws_server.mark_bootstrapped(client_name)
            await self._muika.create_event(
                AdapterOnlineEvent(
                    adapter=adapter,
                )
            )

            if not self.is_bootstraped:
                self.is_bootstraped = True
                self._muika.memory.new_session()
                await self._muika.create_event(SessionBootstrapEvent())

            # 标记适配器为已引导
            return ActionResponse(action=event.type, status="queued")

        if isinstance(event, IpcSessionEndEvent):
            await self._muika.create_event(SessionEndEvent())
            return ActionResponse(action=event.type, status="queued")

        logger.debug(f"[Core] Unknown event type: {event.type}")
        return ErrorMessage(message="unknown_event_type")


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

    recovered = get_core_proposal_manager().recover_incomplete()
    if recovered:
        logger.warning(f"[CoreProposal] Recovered incomplete proposals: {', '.join(recovered)}")
    if mas_config.enable_core_proposals and (mas_config.enable_code_execution or mas_config.enable_shell_execution):
        logger.warning(
            "[CoreProposal] Code or shell execution is enabled. These trusted tools can bypass Core proposal controls."
        )

    if MCP_CONFIG_PATH.exists():
        logger.info("Loading MCP Server config")
        from muika.plugin.mcp import initialize_servers

        await initialize_servers()

    logger.info("Loading plugins...")
    load_plugins(BUILTIN_PLUGINS_PATH, mas_config.plugins_dir)

    bootstrap = CoreBootstrap(host=host, port=port, ipc_secret=mas_config.ipc_secret)
    get_plugin_manager().bind_butler(bootstrap._muika.butler_agent)
    if mas_config.enable_plugin_hot_reload:
        start_plugin_watcher(get_plugin_manager(), Path(mas_config.plugins_dir))

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
