"""验证 Core 启动顺序和事件队列连接。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from muika.core.memory import MemoryCategory, MemoryLayer, MemoryRecord
from muika.ipc.bootstrap import CoreBootstrap


async def test_memory_ready_before_connections_and_scheduler_reaches_loop(monkeypatch):
    for name in ("MuikaBrain", "ButlerAgent", "TopicManager", "DigestAgent", "ReflectionAgent"):
        monkeypatch.setattr(f"muika.core.loop.{name}", MagicMock())
    server = MagicMock()
    server.start = AsyncMock()
    monkeypatch.setattr("muika.ipc.bootstrap.CoreWsServer", MagicMock(return_value=server))
    monkeypatch.setattr("muika.ipc.bootstrap.CommandDispatcher.setup", MagicMock())
    monkeypatch.setattr("muika.ipc.bootstrap.validate_template_configuration", MagicMock())
    bootstrap = CoreBootstrap(ipc_secret="test")
    muika = bootstrap._muika
    entered, release = asyncio.Event(), asyncio.Event()

    async def load_memory():
        entered.set()
        await release.wait()
        muika.memory.records["core:user:name"] = MemoryRecord(
            layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="name", value="Alice"
        )

    monkeypatch.setattr(muika.memory, "load", load_memory)
    monkeypatch.setattr(muika, "start", MagicMock())
    startup = asyncio.create_task(bootstrap.start())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        server.start.assert_not_awaited()
        muika.start.assert_not_called()
        release.set()
        await asyncio.wait_for(startup, 1)
        server.start.assert_awaited_once()
        await bootstrap._handle_event({"type": "session_bootstrap"}, SimpleNamespace(client_name="test"))
        assert not muika.memory.session.is_first_session
        assert muika.memory.records["core:user:name"].value == "Alice"
        while not muika.event_queue.empty():
            await muika.collect_events()
        await bootstrap._executor.scheduler.schedule("scheduled", trigger_in_seconds=0)
        event = await asyncio.wait_for(muika.collect_events(), 1)
        assert event.type == "scheduled_trigger"
        assert event.payload.what == "scheduled"
    finally:
        release.set()
        await startup
        await bootstrap._executor.scheduler.close()


async def test_memory_load_failure_prevents_startup(monkeypatch):
    for name in ("MuikaBrain", "ButlerAgent", "TopicManager", "DigestAgent", "ReflectionAgent"):
        monkeypatch.setattr(f"muika.core.loop.{name}", MagicMock())
    server = MagicMock(start=AsyncMock())
    monkeypatch.setattr("muika.ipc.bootstrap.CoreWsServer", MagicMock(return_value=server))
    monkeypatch.setattr("muika.ipc.bootstrap.CommandDispatcher.setup", MagicMock())
    monkeypatch.setattr("muika.ipc.bootstrap.validate_template_configuration", MagicMock())
    bootstrap = CoreBootstrap(ipc_secret="test")
    bootstrap._muika.memory.load = AsyncMock(side_effect=RuntimeError("history unavailable"))
    bootstrap._muika.start = MagicMock()
    with pytest.raises(RuntimeError, match="history unavailable"):
        await bootstrap.start()
    server.start.assert_not_awaited()
    bootstrap._muika.start.assert_not_called()
