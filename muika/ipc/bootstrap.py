"""Core process entry point.

Usage::

    python -m muika.ipc.bootstrap [--host 127.0.0.1] [--port 8765]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import uuid
from pathlib import Path
from typing import Optional

from pydantic import TypeAdapter

from muika.config import mas_config
from muika.core.events import (
    AdapterOfflineEvent,
    AdapterOnlineEvent,
    CoreChangeEvent,
    ScheduledTriggerEvent,
    ScheduledTriggerPayload,
    SessionBootstrapEvent,
    SessionEndEvent,
    TimeTickEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor, SendReceipt
from muika.core.loop import Muika
from muika.core.self_change import (
    aclose_self_change,
    run_boot_self_change_check,
    setup_self_change,
)
from muika.core.self_mod.proposals import (
    core_maintenance_message,
    get_core_proposal_manager,
    is_core_maintenance_active,
    is_maintenance_command_allowed,
)
from muika.database.db import close_db, init_db
from muika.models import AdapterInfo, Message, Resource
from muika.plugin import CommandDispatcher, load_plugins
from muika.plugin.manager import get_plugin_manager
from muika.plugin.mcp import cleanup_servers, initialize_servers
from muika.plugin.watcher import start_plugin_watcher, stop_plugin_watcher
from muika.template.loader import validate_template_configuration
from muika.utils.logger import init_logger, logger
from muika.utils.utils import get_version

from .attachments import AttachmentTransfer, attachment_routes
from .core_link import CoreLink
from .protocol import ActionResponse, BotToCoreEvent, BotToCoreMessage
from .protocol import CommandEvent as IpcCommandEvent
from .protocol import CommandResult, ErrorMessage, SendMessage
from .protocol import SessionBootstrapEvent as IpcSessionBootstrapEvent
from .protocol import SessionEndEvent as IpcSessionEndEvent
from .protocol import UserMessageEvent as IpcUserMessageEvent
from .server import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer
from .supervisor import RESTART_EXIT_CODE, RestartRecord, lifecycle_directory
from .supervisor import main as supervisor_main
from .supervisor import watch_parent, write_json

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
        self.node: CoreLink | None = None
        self._last_role = ""

        self._ws_server = CoreWsServer(host=host, port=port, secret=ipc_secret)
        self._ws_server.add_routes(attachment_routes(mas_config.data_dir / "chat_attachments", ipc_secret))
        self._attachments = AttachmentTransfer(
            f"ws://{host}:{port}/ws", ipc_secret, mas_config.data_dir / "chat_attachments"
        )

        async def _send_llm_reply(
            content: str, resources: list[Resource] | None = None, target: str | None = None
        ) -> SendReceipt:
            """LLM 对话回复通过 SendMessage 发送，可按 *target* 路由到指定适配器。"""
            resources_dict = [r.to_dict() for r in resources] if resources else []
            msg = SendMessage(content=content, resources=resources_dict)
            return await self._send(msg, target)

        async def _send_command_result(
            content: str, resources: list[dict] | None = None, target: str | None = None
        ) -> None:
            """命令执行结果通过 CommandResult 发送。"""
            msg = CommandResult(content=content, resources=resources or [])
            ok = await self._send(msg, target)
            if not ok:
                logger.warning("[Core] Command result dropped, no Bot connected")

        event_queue: asyncio.Queue = asyncio.Queue()
        self._executor = Executor(event_queue, send_func=_send_llm_reply)
        self._muika = Muika(self._executor, event_queue)

        self._shutdown_event = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.restart_requested = False
        self._restart_task: asyncio.Task[None] | None = None
        if lifecycle_directory() is not None:
            self._muika.restart.handler = self.request_restart
        self.is_bootstraped = False

        CommandDispatcher.setup(self._muika, _send_command_result)

    @property
    def muika(self) -> Muika:
        """提供当前人格和本地记忆，节点连接不拥有认知实现。"""
        return self._muika

    async def _send(self, message: SendMessage | CommandResult, target: str | None) -> SendReceipt:
        if self.node is not None and self.node.connected:
            return await self.node.send(message, target)
        if self.node is not None:
            message = message.model_copy(
                update={"resources": [await self._attachments.upload(Resource(**item)) for item in message.resources]}
            )
        return await self._ws_server.send_to_bot(message, target)

    async def _set_role(self, active: bool, reason: str) -> None:
        if not active:
            await self._executor.scheduler.activate(False)
            if self._muika.is_alive:
                await self._muika.stop()
            return
        if not self._muika.is_alive:
            while not self._muika.event_queue.empty():
                self._muika.event_queue.get_nowait()
            await self._muika.memory.load(record_activity=False)
            if self.node is not None:
                await self.node.store.capture_snapshot()
            self._muika.start()
            await self._executor.scheduler.activate(True)
        if reason != self._last_role:
            self._last_role = reason
            await self._muika.memory.add_material("agent", reason)
            await self._muika.create_event(CoreChangeEvent(reason))

    def _advance_standby(self, seconds: float) -> None:
        if not self._muika.is_alive:
            self._muika.state.tick_state(TimeTickEvent(), seconds)

    async def _from_gateway(self, message: dict, adapter: str) -> None:
        if adapter == "#reminders":
            payload = TypeAdapter(ScheduledTriggerPayload).validate_python(message["payload"])
            await self._muika.create_event(ScheduledTriggerEvent(payload))
            return
        await self._handle_event(message, AdapterInfo(client_name=adapter))

    async def start(self) -> None:
        """加载记忆后启动连接服务和核心循环。"""
        logger.info("Starting Muika...")

        validate_template_configuration((mas_config.persona_template, mas_config.agent_template))

        await self._muika.memory.load()
        setup_self_change(
            self._muika,
            can_send=lambda: (self.node is not None and self.node.connected) or self._ws_server.has_connection,
        )
        self._register_handlers()
        self._register_adapter_callbacks()
        await self._ws_server.start()

        logger.debug(
            f"Muika Core is ready -- ws://{self._host}:{self._port}/ws "
            f"(health: http://{self._host}:{self._port}/health)"
        )
        logger.success("Muika is ready.")
        directory = lifecycle_directory()
        restart_record: Optional[RestartRecord] = None
        if directory is not None:
            write_json(directory / "ready.json", {"pid": os.getpid()})
            record_path = mas_config.data_dir.resolve() / "restart.json"
            if record_path.is_file():
                try:
                    for _ in range(30):
                        record = json.loads(record_path.read_text(encoding="utf-8"))
                        if record.get("status") in {"started", "restored", "failed"}:
                            restart_record = record
                            await self._muika.memory.add_material(
                                "agent",
                                f"Core restart outcome: {record['status']}. "
                                f"Purpose: {record.get('reason', '')}. "
                                "Startup readiness is not full feature verification.",
                                source=f"restart:{record.get('id') or record['patch_id']}:{record['status']}",
                            )
                            break
                        await asyncio.sleep(0.1)
                except (OSError, ValueError, KeyError) as exc:
                    logger.error(f"[Restart] Could not load the saved restart outcome: {exc}")
        await run_boot_self_change_check(self._muika, restart_record)
        if mas_config.gateway_url:
            self._executor.scheduler.persistent = True
            self._executor.scheduler.active = False
            self.node = CoreLink(self._muika.state, self._set_role, self._from_gateway, self._advance_standby)
            self._executor.scheduler.relay_trigger = self.node.relay_reminder
            await self.node.start()
            self._muika.after_activity = self.node.store.capture_snapshot
            self._muika.state.nodes = self.node
        else:
            self._muika.start()

    async def request_restart(self, patch_id: str | None, trigger: str) -> None:
        """暂停行动，按需应用指定提案，再请求父进程重启。"""
        if self._restart_task is not None and not self._restart_task.done():
            raise ValueError("A restart is already pending.")
        directory = lifecycle_directory()
        if directory is None:
            raise ValueError("A supervisor is required to restart Core.")
        manager = get_core_proposal_manager()
        proposal = manager.check_ready(patch_id) if patch_id is not None else None
        record_path = mas_config.data_dir.resolve() / "restart.json"
        record: RestartRecord = {
            "id": uuid.uuid4().hex,
            "patch_id": patch_id,
            "reason": proposal["reason"] if proposal is not None else trigger,
            "trigger": trigger,
            "proposal_file": str(manager.proposals_root / patch_id / "proposal.json") if patch_id is not None else None,
            "record_path": str(record_path),
            "status": "preparing",
        }

        async def restart() -> None:
            try:
                await self._muika.agent_tasks.handoff()
                if patch_id is not None:
                    manager.check_ready(patch_id)
                write_json(record_path, record)
                if patch_id is not None:
                    await manager.apply(patch_id)
                record["status"] = "restarting"
                write_json(record_path, record)
                write_json(directory / "restart.json", record)
                self.restart_requested = True
                self.stop_requested.set()
            except Exception as exc:
                record["status"] = "failed"
                record["error"] = str(exc)
                write_json(record_path, record)
                await self._muika.agent_tasks.release_persona()
                await self._executor.send_message(f"这次还不能重启：{exc}")
                logger.exception("[Restart] Could not prepare the requested restart.")

        self._restart_task = asyncio.create_task(restart())

    async def stop(self) -> None:
        """停止接入和后台活动，再关闭数据库。"""
        if self._shutdown_event.is_set():
            return

        logger.info("Stopping Muika...")

        self._shutdown_event.set()
        if self._restart_task is not None and not self._restart_task.done():
            self._restart_task.cancel()
            await asyncio.gather(self._restart_task, return_exceptions=True)
        try:
            if self.node is not None:
                await self.node.close()
            await self._ws_server.stop()
        finally:
            stop_plugin_watcher()
            get_plugin_manager().shutdown_all()
            await self._muika.stop()
            await aclose_self_change()
            await self._executor.scheduler.close()
            await close_db()

        logger.success("Muika stopped.")

    def _register_handlers(self) -> None:
        for msg_type in ("user_message", "command", "session_bootstrap", "session_end"):
            self._ws_server.register_handler(msg_type, self._handle_event)

    def _register_adapter_callbacks(self) -> None:
        """注册适配器连接 / 断开回调，将事件推入 Muika 事件队列。"""

        async def _on_adapter_connected(adapter: AdapterInfo) -> None:
            logger.debug(f"[Core] Adapter online: {adapter!r}")
            if self.is_bootstraped and self._muika.is_alive:
                await self._muika.create_event(AdapterOnlineEvent(adapter=adapter))

        async def _on_adapter_disconnected(adapter: AdapterInfo) -> None:
            logger.debug(f"[Core] Adapter offline: {adapter!r}")
            if self.is_bootstraped and self._muika.is_alive:
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

        logger.debug(f"[Core] Received event: {event.type} from {client_name!r}")
        if self.node is not None and not self.node.active:
            if self.node.sync_error:
                return ErrorMessage(
                    message="history_sync_failed", detail=f"历史同步失败，暂时不能接管：{self.node.sync_error}"
                )
            return ErrorMessage(message="inactive_core", detail="备用设备正在同步，请连接常驻入口。")
        source = "ipc:" + json.dumps([client_name, event.id], separators=(",", ":")) if self.node else None
        if source and await self._muika.memory.contains_source(source):
            return ActionResponse(action=event.type, status="observed")
        if source and not isinstance(event, IpcUserMessageEvent):
            await self._muika.memory.add_material("note", f"Adapter event: {event.type}", source=source)

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
            resources = []
            for item in event.resources:
                resource = Resource(**item)
                if resource.url and "/attachments/" in resource.url:
                    transfer = self.node.attachments if self.node and self.node.connected else self._attachments
                    resource = await transfer.download(resource)
                resources.append(resource)
            msg = Message(message=event.message, resources=resources)
            await self._muika.create_event(UserMessageEvent(UserMessagePayload(msg, source=source)))
            return ActionResponse(action=event.type, status="queued")

        if isinstance(event, IpcCommandEvent):
            self._ws_server.set_triggering_adapter(client_name)
            await CommandDispatcher.get().dispatch(event.raw)
            return ActionResponse(action=event.type, status="ok")

        if isinstance(event, IpcSessionBootstrapEvent):
            logger.debug(f"[Core] Adapter {client_name!r} joined existing session")
            self._ws_server.mark_bootstrapped(client_name)
            await self._muika.create_event(
                AdapterOnlineEvent(
                    adapter=adapter,
                )
            )

            if not self.is_bootstraped:
                self.is_bootstraped = True
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
    """初始化核心进程，并在退出或启动失败后清理资源。"""
    init_logger()

    logger.info(f"Muika-After-Story version: {get_version()}")
    logger.debug(f"Muika-After-Story data directory: {mas_config.data_dir.resolve()}")

    logger.debug("Loading Database...")
    await init_db()

    bootstrap: Optional[CoreBootstrap] = None
    try:
        recovered = get_core_proposal_manager().recover_incomplete()
        if recovered:
            logger.warning(f"[CoreProposal] Recovered incomplete proposals: {', '.join(recovered)}")
        logger.info(f"[Permissions] {mas_config.action_permission}; code review: {mas_config.code_review_mode}.")

        if MCP_CONFIG_PATH.exists():
            logger.debug("Loading MCP Server config")

            await initialize_servers()

        logger.info("Loading plugins...")
        load_plugins(BUILTIN_PLUGINS_PATH, mas_config.plugins_dir)

        bootstrap = CoreBootstrap(host=host, port=port, ipc_secret=mas_config.ipc_secret)
        if mas_config.enable_plugin_hot_reload:
            start_plugin_watcher(get_plugin_manager(), Path(mas_config.plugins_dir))

        stop_event = bootstrap.stop_requested
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass

        await bootstrap.start()
        await stop_event.wait()
    except KeyboardInterrupt:
        logger.info("Muika was stopped by the user.")
    finally:
        try:
            if bootstrap is not None:
                await bootstrap.stop()
            else:
                stop_plugin_watcher()
                get_plugin_manager().shutdown_all()
                await close_db()
        finally:
            await cleanup_servers()
    if bootstrap is not None and bootstrap.restart_requested:
        raise SystemExit(RESTART_EXIT_CODE)


def main(argv: Optional[list[str]] = None) -> None:
    """解析命令行参数并运行核心进程。"""
    if lifecycle_directory() is None:
        supervisor_main(argv)
        return
    watch_parent()
    args = _parse_args(argv)
    asyncio.run(run_core(host=args.host, port=args.port))


if __name__ == "__main__":
    main()
