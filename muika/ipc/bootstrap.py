"""Core process entry point.

Usage::

    python -m muika.ipc.bootstrap [--host 127.0.0.1] [--port 8765]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from muika.core.events import (
    SessionBootstrapEvent,
    SessionEndEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.core.state import MuikaState
from muika.ipc import DEFAULT_HOST, DEFAULT_PORT, CoreWsServer, SendMessage, StateUpdate
from muika.models import Message
from muika.utils.logger import init_logger, logger


class CoreBootstrap:
    """Wires up the WS server, Executor, and Muika engine.

    :param host: WebSocket listen address.
    :param port: WebSocket listen port.
    :param data_dir: directory for runtime data (connection records, etc.).
    """

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        data_dir: Optional[Path] = None,
    ) -> None:
        self._host = host
        self._port = port
        self._data_dir = data_dir or Path(".")

        self._ws_server = CoreWsServer(host=host, port=port)

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

    async def start(self) -> None:
        """Boot all components."""
        logger.info("Muika Core is booting...")
        self._data_dir.mkdir(exist_ok=True, parents=True)

        self._register_handlers()
        await self._ws_server.start()

        self._muika.start()

        self._state_push_task = asyncio.create_task(self._push_state_periodically())
        logger.success(
            f"Muika Core is ready -- ws://{self._host}:{self._port}/ws "
            f"(health: http://{self._host}:{self._port}/health)"
        )

    async def stop(self) -> None:
        """Gracefully shut down all components."""
        logger.info("Muika Core is shutting down...")
        self._shutdown_event.set()

        self._muika.stop()

        if hasattr(self, "_state_push_task"):
            self._state_push_task.cancel()
            try:
                await self._state_push_task
            except asyncio.CancelledError:
                pass

        await self._ws_server.stop()

        from muika.database.db import close_db

        await close_db()

        logger.success("Muika Core stopped")

    def _register_handlers(self) -> None:
        """Register WS message handlers."""
        self._ws_server.register_handler("event", self._handle_event)
        self._ws_server.register_handler("query", self._handle_query)
        self._ws_server.register_handler("debug", self._handle_debug)
        self._ws_server.register_handler("config_changed", self._handle_config_changed)

    async def _handle_event(self, message: dict) -> Optional[dict]:
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
            return {"status": "queued", "event_type": event_type}

        if event_type == "session_bootstrap":
            self._muika.memory.new_session()
            last_chat_str = payload.get("last_chat_time")
            last_chat = datetime.fromisoformat(last_chat_str) if last_chat_str else None
            await self._muika.create_event(SessionBootstrapEvent(last_chat_time=last_chat))
            return {"status": "bootstrapped"}

        if event_type == "session_end":
            await self._muika.create_event(SessionEndEvent())
            return {"status": "session_ended"}

        logger.debug(f"[Core] Unknown event type: {event_type}")
        return {"status": "unknown_event", "event_type": event_type}

    async def _handle_query(self, message: dict) -> Optional[dict]:
        """Handle state queries from the Bot."""
        query_type = message.get("query", "state")
        if query_type == "state":
            return {"query": "state", "data": _serialize_state(self._muika.state)}
        return {"query": query_type, "data": {}}

    async def _handle_debug(self, message: dict) -> Optional[dict]:
        """Handle debug commands from the Bot."""
        action = message.get("action", "")
        field = message.get("field")
        value = message.get("value")
        logger.info(f"[Core] Debug action: {action}")

        if action == "trigger_topic":
            await self._muika._run_topic_pipeline()
            return {"action": "trigger_topic", "status": "ok"}

        if action == "set_state" and field and value is not None:
            _apply_state_field(self._muika.state, field, value)
            return {"action": "set_state", "field": field, "status": "ok"}

        if action == "reset_topic":
            self._muika.state.active_topic = None
            return {"action": "reset_topic", "status": "ok"}

        return {"action": action, "status": "unknown_action"}

    async def _handle_config_changed(self, message: dict) -> Optional[dict]:
        """Handle model config change notification from the Bot."""
        config_name = message.get("config_name")
        logger.info(f"[Core] Config changed: {config_name}")
        try:
            from muika.config import get_model_config_manager

            manager = get_model_config_manager()
            manager._on_config_changed()
        except Exception as e:
            logger.warning(f"[Core] Failed to reload model: {e}")
        return {"status": "acknowledged"}

    async def _push_state_periodically(self, interval: float = 5.0) -> None:
        """Push MuikaState snapshots to the Bot at regular intervals."""
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=interval)
                break
            except asyncio.TimeoutError:
                if self._ws_server.has_connection:
                    state_dict = _serialize_state(self._muika.state)
                    await self._ws_server.send_to_bot(StateUpdate(state=state_dict))


def _serialize_state(s: MuikaState) -> dict:
    """Convert a MuikaState to a JSON-serializable dict."""
    at = s.active_topic
    return {
        "mood": s.mood,
        "attention": s.attention,
        "loneliness": s.loneliness,
        "boredom": s.boredom,
        "curiosity": s.curiosity,
        "last_interaction": s.last_interaction.isoformat() if s.last_interaction else None,
        "last_proactive_at": s.last_proactive_at.isoformat() if s.last_proactive_at else None,
        "active_topic": (
            {
                "topic_id": at.topic_id,
                "topic_type": at.topic_type,
                "topic_seed": at.topic_seed,
                "started_at": at.started_at.isoformat() if at else None,
                "user_engaged": at.user_engaged,
            }
            if at
            else None
        ),
    }


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
    parser.add_argument(
        "--data-dir",
        default=os.getenv("MUIKA_DATA_DIR", "."),
        help="Data directory path (default: cwd)",
    )
    return parser.parse_args(argv)


async def run_core(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    data_dir: Optional[Path] = None,
) -> None:
    """Async entry point for the Core process."""
    init_logger()

    from muika.database.db import init_db

    await init_db()

    bootstrap = CoreBootstrap(host=host, port=port, data_dir=data_dir)
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
    data_dir = Path(args.data_dir) if args.data_dir else None
    asyncio.run(run_core(host=args.host, port=args.port, data_dir=data_dir))


if __name__ == "__main__":
    main()
