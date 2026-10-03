"""验证设备感知和交接故障后的关系连续性。

失败边界：空闲时设备变化不可见；重复注册引发重复思考；目标消失留下交接循环；
状态重启丢失交接偏好；取得租约被误记为恢复成功；抖动改变候选优先级。
"""

import asyncio
import hashlib

import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn
from sqlalchemy import select

from muika.core.devices import ExecutionEnvironment
from muika.database.db import close_db, get_session, init_db
from muika.database.orm_models import ExperienceORM, RuntimeInboxORM
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    Acquire,
    Claim,
    CoreReady,
    Handoff,
    LoadRuntime,
    Pending,
    Receive,
    RegisterNode,
    Release,
    Renew,
    SaveRuntime,
    Status,
)
from muika.ipc.state_server import NodeCredential, StateServer
from muika.node.core_node import CoreNode
from muika.node.models import IncomingMessage
from muika.node.turn_protocol import ClientRoute, RuntimeSnapshot

pytestmark = pytest.mark.e2e


def credentials():
    return [
        NodeCredential(id=name, role=role, priority=priority, token_sha256=hashlib.sha256(name.encode()).hexdigest())
        for name, role, priority in (
            ("pc", "core", 0),
            ("server", "core", 10),
            ("device", "executor", 0),
            ("chat", "bot", 0),
        )
    ]


async def observations():
    async with get_session() as db:
        rows = await db.scalars(select(RuntimeInboxORM).order_by(RuntimeInboxORM.sequence))
        return [(IncomingMessage.model_validate_json(row.payload), row.status) for row in rows]


async def wait_processed(type, count=1):
    async with asyncio.timeout(15):
        while True:
            found = [
                message
                for message, status in await observations()
                if message.event and message.event.type == type and status == "processed"
            ]
            if len(found) >= count:
                return found
            await asyncio.sleep(0.05)


async def test_idle_device_changes_reach_muika_without_forced_replies(monkeypatch, tmp_path, recorder):
    app = CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text="我记住啦。")])
    app.scripted.add_route(
        when="[Runtime observation]",
        text="<heart>I can notice this and stay quietly with you.</heart><do_nothing>",
        name="quiet_observation",
    )
    await init_db(tmp_path / "state.db")
    state = StateServer(credentials())
    service = TestServer(state.app)
    job = None
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        core = CoreNode(address, "pc", "pc", tmp_path / "pc")
        job = asyncio.create_task(core.run())
        await asyncio.wait_for(core.ready.wait(), 10)
        async with NodeClient(address, "chat") as bot:
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="remember", client_id="chat", conversation_id="original", text="记住我们。"
                    )
                )
            )
            async with asyncio.timeout(10):
                while not (await bot.request(Pending())).replies:
                    await asyncio.sleep(0.05)
            async with NodeClient(address, "device") as device:
                registration = RegisterNode(
                    environment=ExecutionEnvironment.local().model_copy(update={"action_permission": "read_only"})
                )
                await device.request(registration)
                online = await wait_processed("device_online")
                await device.request(RegisterNode(environment=registration.environment))
                await device.request(
                    RegisterNode(environment=registration.environment.model_copy(update={"action_permission": "write"}))
                )
                changed = await wait_processed("device_capability_changed")
            offline = await wait_processed("device_offline")
            all_events = [
                message
                for message, _ in await observations()
                if message.event and message.event.type.startswith("device_")
            ]
            assert len(all_events) == 3
            assert all(message.conversation_id == "original" for message in all_events)
            assert len((await bot.request(Pending())).replies) == 1
            async with get_session() as db:
                experiences = list(await db.scalars(select(ExperienceORM)))
                assert all(
                    any(message.event.report == row.content for row in experiences)
                    for message in [*online, *changed, *offline]
                )
            recorder.record(
                "checked",
                invariant="idle_device_changes_are_observed_once_and_can_stay_silent",
                reports=[message.event.report for message in all_events],
            )
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await service.close()
        await close_db()


async def test_disappearing_target_does_not_restart_active_core(monkeypatch, tmp_path, recorder):
    app = CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text="我记住啦。"), ScriptedTurn(text="我还在这里。")])
    app.scripted.add_route(when="[Runtime observation]", text="<do_nothing>", name="observe_handoff")
    await init_db(tmp_path / "state.db")
    state = StateServer(credentials())
    service = TestServer(state.app)
    job = None
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        core = CoreNode(address, "pc", "pc", tmp_path / "pc")
        job = asyncio.create_task(core.run())
        await asyncio.wait_for(core.ready.wait(), 10)
        async with NodeClient(address, "chat") as bot, NodeClient(address, "server") as target:
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="remember", client_id="chat", conversation_id="original", text="记住我们。"
                    )
                )
            )
            async with asyncio.timeout(10):
                while not (await bot.request(Pending())).replies:
                    await asyncio.sleep(0.05)
            dispatch = state.dispatch

            async def disconnect_at_handoff(node, request):
                if isinstance(request, Handoff):
                    await target.close()
                    async with asyncio.timeout(5):
                        while "server" in state.connections:
                            await asyncio.sleep(0.01)
                return await dispatch(node, request)

            monkeypatch.setattr(state, "dispatch", disconnect_at_handoff)
            epoch = core.epoch()
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="move",
                        client_id="chat",
                        conversation_id="original",
                        kind="command",
                        text=".nodes handoff server",
                    )
                )
            )
            result = await wait_processed("core_handoff_result")
            assert result[0].event.status == "failed"
            assert core.ready.is_set() and core.epoch() == epoch
            assert core.runtime_snapshot().handoff_target is None
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="return", client_id="chat", conversation_id="original", text="你还在吗？"
                    )
                )
            )
            async with asyncio.timeout(10):
                while not any("还在这里" in reply.text for reply in (await bot.request(Pending())).replies):
                    await asyncio.sleep(0.05)
            recorder.record(
                "checked",
                invariant="failed_handoff_closes_request_and_keeps_conversation",
                result=result[0].event.report,
                epoch=epoch,
            )
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await service.close()
        await close_db()


async def test_restart_preserves_preference_and_confirms_only_ready_core(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = TestServer(StateServer(credentials()).app)
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server"):
            epoch = (await pc.request(Acquire())).lease.epoch
            await pc.request(
                SaveRuntime(
                    epoch=epoch,
                    runtime=RuntimeSnapshot(
                        route=ClientRoute(client_id="chat", conversation_id="original"),
                        handoff_target="server",
                        handoff_id="move",
                    ),
                )
            )
            assert (await pc.request(Handoff(epoch=epoch, target="server"))).handoff_accepted
            assert (await pc.request(Acquire())).lease is None
        await service.close()
        service = TestServer(StateServer(credentials()).app)
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as target:
            with pytest.raises(NodeRequestError, match="expired|changed"):
                await pc.request(Renew(epoch=epoch))
            assert (await pc.request(Acquire())).lease is None
            lease = (await target.request(Acquire())).lease
            assert lease is not None
            assert not any(
                message.event and message.event.type == "core_handoff_result" for message, _ in await observations()
            )
            await target.request(CoreReady(epoch=lease.epoch))
            await target.request(CoreReady(epoch=lease.epoch))
            results = [
                message
                for message, _ in await observations()
                if message.event and message.event.type == "core_handoff_result"
            ]
            assert len(results) == 1 and results[0].event.status == "completed"
            claim = (await target.request(Claim(epoch=lease.epoch))).claim
            assert claim.message.id == results[0].id
            recorder.record(
                "checked",
                invariant="restart_keeps_target_preference_and_ready_confirmation_is_idempotent",
                result=results[0].event.report,
                epoch=lease.epoch,
            )
    finally:
        await service.close()
        await close_db()


async def test_jitter_preserves_candidate_order_and_missing_target_can_fall_back(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = TestServer(StateServer(credentials()).app)
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as target:
            await asyncio.sleep(3)
            assert (await target.request(Acquire())).lease is None
            epoch = (await pc.request(Acquire())).lease.epoch
            await pc.request(
                SaveRuntime(
                    epoch=epoch,
                    runtime=RuntimeSnapshot(
                        route=ClientRoute(client_id="chat", conversation_id="original"),
                        handoff_target="server",
                        handoff_id="unreachable",
                    ),
                )
            )
            await pc.request(Handoff(epoch=epoch, target="server"))
            await target.close()
            await asyncio.sleep(5.1)
            lease = (await pc.request(Acquire())).lease
            assert lease is not None
            await pc.request(CoreReady(epoch=lease.epoch))
            results = [
                message
                for message, _ in await observations()
                if message.event and message.event.type == "core_handoff_result"
            ]
            assert len(results) == 1 and results[0].event.status == "failed"
            assert (await pc.request(Status())).lease.owner == "pc"
            recorder.record(
                "checked",
                invariant="jitter_keeps_priority_and_expired_handoff_preference_allows_fallback",
                result=results[0].event.report,
            )
    finally:
        await service.close()
        await close_db()


async def test_interrupted_request_and_failed_startup_close_on_recovery(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = TestServer(StateServer(credentials()).app)
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as pc, NodeClient(address, "server") as target:
            epoch = (await pc.request(Acquire())).lease.epoch
            route = ClientRoute(client_id="chat", conversation_id="original")
            await pc.request(
                SaveRuntime(
                    epoch=epoch,
                    runtime=RuntimeSnapshot(
                        route=route,
                        handoff_target="server",
                        handoff_id="interrupted-request",
                    ),
                )
            )
            await pc.request(Release(epoch=epoch))
            epoch = (await pc.request(Acquire())).lease.epoch
            recovered = (await pc.request(LoadRuntime(epoch=epoch))).runtime
            assert recovered.handoff_target is None and recovered.handoff_id is None
            await pc.request(CoreReady(epoch=epoch))
            await pc.request(
                SaveRuntime(
                    epoch=epoch,
                    runtime=RuntimeSnapshot(
                        route=route,
                        handoff_target="server",
                        handoff_id="startup-failure",
                    ),
                )
            )
            await pc.request(Handoff(epoch=epoch, target="server"))
            grant = (await target.request(Acquire(duration=0.2))).lease
            assert grant is not None
            await target.close()
            await asyncio.sleep(0.3)
            recovered = (await pc.request(Acquire())).lease
            assert recovered is not None
            await pc.request(CoreReady(epoch=recovered.epoch))
            results = [
                message
                for message, _ in await observations()
                if message.event and message.event.type == "core_handoff_result"
            ]
            assert len(results) == 2 and all(message.event.status == "failed" for message in results)
            assert len({message.id for message in results}) == 2
            recorder.record(
                "checked",
                invariant="unaccepted_request_and_failed_startup_have_distinct_failure_results",
                reports=[message.event.report for message in results],
            )
    finally:
        await service.close()
        await close_db()
