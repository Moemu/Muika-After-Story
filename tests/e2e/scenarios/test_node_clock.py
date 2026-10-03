"""验证不同时区的 Core 重投同一超时事件不会产生第二次触发。"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestServer

from muika.core.events import TimeoutEvent
from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient
from muika.ipc.node_protocol import Acquire, Claim, Handoff
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.core_node import CoreNode
from muika.node.turn_protocol import ClientRoute

pytestmark = pytest.mark.e2e


async def test_timeout_retransmission_keeps_absolute_identity(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = TestServer(
        StateServer(
            [
                NodeCredential(id=name, role="core", token_sha256=hashlib.sha256(name.encode()).hexdigest())
                for name in ("pc", "server")
            ]
        ).app
    )
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as server:
            origin = datetime(2026, 10, 2, 12, tzinfo=timezone(timedelta(hours=8)))
            first = CoreNode(address, "pc", "pc", tmp_path / "pc")
            first.client, first.lease = pc, (await pc.request(Acquire())).lease
            assert first.lease is not None
            first.snapshot.route = ClientRoute(client_id="chat", conversation_id="original")
            await first.publish(TimeoutEvent(origin, 60))
            await pc.request(Handoff(epoch=first.epoch(), target="server"))
            resumed = CoreNode(address, "server", "server", tmp_path / "server")
            resumed.client, resumed.lease = server, (await server.request(Acquire())).lease
            assert resumed.lease is not None
            resumed.snapshot.route = first.snapshot.route
            await resumed.publish(TimeoutEvent(origin.astimezone(timezone.utc), 60))
            claim = (await server.request(Claim(epoch=resumed.epoch()))).claim
            assert claim is not None and claim.message.id == "timeout:2026-10-02T04:00:00+00:00"
            await resumed.complete_empty(claim)
            assert (await server.request(Claim(epoch=resumed.epoch()))).claim is None
            recorder.record("checked", invariant="different_timezone_timeout_retransmits_one_absolute_event")
    finally:
        await service.close()
        await close_db()
