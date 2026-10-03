"""验证发布审查发现的路由、重试和提交边界。

失败边界：提案误发给执行设备；阅读状态保存后被重复应用；网络失败误记为未知动作；
检查点抖动终结有效任期；失效租约继续运行；话题和控制状态先于回复生效；
两个候选同时取得租约；旧 Core 在接管后认领新输入。
"""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime

import pytest
from aiohttp.test_utils import TestServer
from harness.core_app import CoreApp
from harness.scripted_llm import ScriptedTurn
from sqlalchemy.exc import OperationalError

from muika.core.actions.tools import _info
from muika.core.devices import ExecutionEnvironment
from muika.core.events import TimeTickEvent
from muika.core.executor import Executor
from muika.core.loop import Muika
from muika.core.self_mod import proposals
from muika.core.topic_manager import BaseTopic, TopicSource
from muika.database.db import close_db, init_db
from muika.ipc.node_client import NodeClient, NodeRequestError
from muika.ipc.node_protocol import (
    Acquire,
    Claim,
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
from muika.llm._schema import ToolCall
from muika.node.core_node import CoreNode
from muika.node.executor_node import ExecutorNode, ExecutorWorker, capabilities
from muika.node.models import IncomingMessage
from muika.node.remote_memory import RemoteMemoryManager
from muika.node.turn_protocol import CompleteTurn
from muika.plugin.func_call.context import tool_context

pytestmark = pytest.mark.e2e


def credentials():
    return [
        NodeCredential(id=name, role=role, token_sha256=hashlib.sha256(name.encode()).hexdigest())
        for name, role in (("pc", "core"), ("server", "core"), ("chat", "bot"), ("device", "executor"))
    ]


async def wait_reply(bot, text):
    async with asyncio.timeout(15):
        while True:
            replies = (await bot.request(Pending())).replies
            if any(text in reply.text for reply in replies):
                return replies
            await asyncio.sleep(0.05)


@pytest.fixture
async def running_core(monkeypatch, tmp_path, recorder):
    app = CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text="我记住啦。")])
    app.scripted.add_route(when="[Runtime observation]", text="<do_nothing>", name="quiet_observation")
    await init_db(tmp_path / "state.db")
    state = StateServer(credentials())
    service = TestServer(state.app)
    job = None
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "chat") as bot:
            core = CoreNode(address, "pc", "pc", tmp_path / "pc", lease_seconds=1.5)
            job = asyncio.create_task(core.run())
            await asyncio.wait_for(core.ready.wait(), 10)
            await bot.request(
                Receive(
                    message=IncomingMessage(id="warmup", client_id="chat", conversation_id="master", text="我来了。")
                )
            )
            await wait_reply(bot, "我记住啦")
            yield core, state, bot, app
    finally:
        if job is not None:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
        await service.close()
        await close_db()


async def test_proposal_tools_stay_on_core_with_a_remote_executor(running_core, monkeypatch, tmp_path, recorder):
    core, _, _, _ = running_core
    monkeypatch.setattr(proposals, "_manager", proposals.CoreProposalManager())
    executor = ExecutorNode(core.address, "device", "device", tmp_path / "device")
    job = asyncio.create_task(executor.run())
    try:
        await asyncio.wait_for(executor.ready.wait(), 10)
        await core.select_device("device")
        nodes = (await core.connection().request(Status())).nodes
        names = {
            "core_list",
            "core_read",
            "core_search",
            "propose_core_change",
            "prepare_core_change",
            "discard_core_change",
        }
        device = next(node for node in nodes if node.id == "device")
        assert names.isdisjoint(device.tools)
        result = await core.execute_tool(
            ToolCall(
                id="observe-core",
                name="core_read",
                arguments=json.dumps({"path": "muika/core/state.py", "line_start": 1, "line_end": 50}),
            )
        )
        assert not result.is_error and "class MuikaState" in result.text
        assert core.worker is not None
        records = core.worker.ledger.db.execute("SELECT payload FROM execution").fetchall()
        assert any(json.loads(row[0])["spec"]["node_id"] == "pc" for row in records)
        recorder.record("checked", invariant="proposal_tools_belong_to_core_despite_selected_remote_device")
    finally:
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.parametrize("failure", ["connection", "checkpoint"])
async def test_reading_state_is_not_replayed_after_an_uncertain_completion(monkeypatch, tmp_path, recorder, failure):
    CoreApp(monkeypatch, recorder)
    await init_db(tmp_path / "state.db")
    service = TestServer(StateServer(credentials()).app)
    worker = None
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with NodeClient(address, "pc") as client:
            await client.request(RegisterNode(tools=capabilities(), environment=ExecutionEnvironment.local()))
            core = CoreNode(address, "pc", "pc", tmp_path / "pc")
            core.client, core.lease = client, (await client.request(Acquire())).lease
            worker = ExecutorWorker(client, tmp_path / "pc")
            core.worker = worker
            memory = RemoteMemoryManager(client, core.epoch(), tmp_path / "resources")
            await memory.load()
            core.muika = Muika(
                Executor(asyncio.Queue(), core.send_message), asyncio.Queue(), memory=memory, runtime=core
            )
            core.muika.state.boredom, core.muika.state.curiosity, core.muika.state.attention = 0.8, 0.2, 0.4
            core.snapshot.selected_executor = "pc"
            source = next(iter(_info.RSS_SOURCES))

            async def unavailable(url):
                raise OSError("RSS connection unavailable")

            monkeypatch.setattr(_info, "fetch_rss_content", unavailable)
            with tool_context(core.muika.state, core.muika.executor):
                failed = await core.execute_tool(
                    ToolCall(
                        id="network-failure", name="check_rss_update", arguments=json.dumps({"rss_source": source})
                    )
                )
            assert failed.is_error and failed.outcome == "not_executed"
            assert core.muika.state.curiosity == 0.2

            async def feed(url):
                return (
                    "<rss version='2.0'><channel><title>Poetry</title>"
                    "<item><title>A poem</title></item></channel></rss>"
                )

            monkeypatch.setattr(_info, "fetch_rss_content", feed)
            original_commit = core.commit_tool_state

            async def lose_result():
                await original_commit()
                if failure == "checkpoint":
                    raise NodeRequestError(
                        "Runtime checkpoint is temporarily unavailable.",
                        code="checkpoint_unavailable",
                    )
                raise ConnectionError("Lost result after the state checkpoint")

            monkeypatch.setattr(core, "commit_tool_state", lose_result)
            call = ToolCall(id="read-once", name="check_rss_update", arguments=json.dumps({"rss_source": source}))
            with tool_context(core.muika.state, core.muika.executor):
                if failure == "connection":
                    with pytest.raises(ConnectionError, match="Lost result"):
                        await core.execute_tool(call)
                else:
                    uncertain = await core.execute_tool(call)
                    assert uncertain.is_error and uncertain.outcome == "unknown"
            saved = (await client.request(LoadRuntime(epoch=core.epoch()))).runtime
            assert saved is not None and saved.curiosity == pytest.approx(0.4)
            await client.request(Release(epoch=core.epoch()))
            core.lease = (await client.request(Acquire())).lease
            with tool_context(core.muika.state, core.muika.executor):
                retry = await core.execute_tool(call)
            assert retry.outcome == "unknown"
            assert core.muika.state.curiosity == pytest.approx(0.4)
            assert core.muika.state.boredom == pytest.approx(0.24)
            declared = next(node for node in (await client.request(Status())).nodes if node.id == "pc")
            assert all(
                tool.retry == "verify"
                for tool in declared.capabilities
                if tool.name in {"check_rss_update", "search_wikipedia", "web_search"}
            )
            recorder.record(
                "checked",
                invariant="reading_drives_change_once_and_network_failure_is_known_not_executed",
                curiosity=saved.curiosity,
            )
    finally:
        if worker is not None:
            await worker.close()
        await service.close()
        await close_db()


@pytest.mark.parametrize("failure", ["checkpoint", "checkpoint_legacy", "lease", "checkpoint_rejected"])
async def test_checkpoint_failure_and_lease_failure_have_distinct_lifetimes(
    running_core, monkeypatch, tmp_path, recorder, failure
):
    core, state, _, _ = running_core
    epoch = core.epoch()
    original = state.dispatch
    injected, recovered = asyncio.Event(), asyncio.Event()
    db_path = tmp_path / "busy.db"
    with closing(sqlite3.connect(db_path)) as first, closing(sqlite3.connect(db_path, timeout=0)) as second:
        first.execute("CREATE TABLE marker (id INTEGER)")
        first.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as failure_info:
            second.execute("INSERT INTO marker VALUES (1)")
        busy = OperationalError("checkpoint", {}, failure_info.value)
    if failure == "checkpoint_legacy":
        busy = OperationalError("checkpoint", {}, sqlite3.OperationalError("database is locked"))

    async def dispatch(node, request):
        if (
            node.id == "pc"
            and not injected.is_set()
            and isinstance(request, Renew if failure == "lease" else SaveRuntime)
        ):
            injected.set()
            if failure in {"checkpoint", "checkpoint_legacy"}:
                raise busy
            raise ValueError("Core lease has expired or changed.")
        response = await original(node, request)
        if node.id == "pc" and injected.is_set() and isinstance(request, SaveRuntime):
            recovered.set()
        return response

    monkeypatch.setattr(state, "dispatch", dispatch)
    await asyncio.wait_for(injected.wait(), 5)
    await asyncio.wait_for(recovered.wait(), 10)
    assert core.ready.is_set()
    assert (core.epoch() == epoch) == (failure in {"checkpoint", "checkpoint_legacy"})
    recorder.record(
        "checked",
        invariant="checkpoint_failure_preserves_valid_authority_but_renew_failure_restarts",
        failure=failure,
        previous_epoch=epoch,
        current_epoch=core.epoch(),
    )


@pytest.mark.parametrize("pipeline", ["topic", "brain"])
async def test_failed_reply_does_not_publish_pending_topic_or_controls(running_core, monkeypatch, recorder, pipeline):
    core, state, bot, app = running_core
    assert core.muika is not None and state.turn_service is not None
    app.scripted.add_route(when=lambda request: True, text="想和你一起读诗。<timeout: 60s>", name="poetry")

    async def topic(state):
        return BaseTopic(id="poetry", source=TopicSource.STATIC, category="relationship", content="一起读诗")

    monkeypatch.setattr(core.muika.topic_manager, "get_next_topic", topic)
    core.muika.state.boredom = 0.8
    original = state.turn_service.execute
    checked = asyncio.Event()

    async def reject_first(db, owner, epoch, action):
        if (
            isinstance(action, CompleteTurn)
            and action.content
            and "一起读诗" in action.content
            and not checked.is_set()
        ):
            assert core.muika is not None
            assert core.muika.state.active_topic is None
            assert core.muika.runtime_controls().timeout_set_at is None
            if pipeline == "topic":
                assert core.muika.state.boredom > 0
            checked.set()
            raise ValueError("Injected reply commit rejection")
        return await original(db, owner, epoch, action)

    monkeypatch.setattr(state.turn_service, "execute", reject_first)
    if pipeline == "topic":
        await core.publish(TimeTickEvent(timestamp=datetime.now(), think_mode="topic"))
    else:
        await bot.request(
            Receive(
                message=IncomingMessage(id="poetry", client_id="chat", conversation_id="master", text="我们读诗吧。")
            )
        )
    replies = await wait_reply(bot, "一起读诗")
    assert checked.is_set()
    assert len([reply for reply in replies if "一起读诗" in reply.text]) == 1
    assert core.muika is not None
    assert core.muika.runtime_controls().timeout_set_at is not None
    saved = (await core.connection().request(LoadRuntime(epoch=core.epoch()))).runtime
    assert saved is not None and saved.timeout_set_at == core.muika.runtime_controls().timeout_set_at
    if pipeline == "topic":
        assert core.muika.state.active_topic is not None and saved.active_topic is not None
        assert core.muika.state.active_topic.topic_id == saved.active_topic.topic_id == "poetry"
    recorder.record(
        "checked", invariant="reply_commit_publishes_topic_and_controls_only_after_success", pipeline=pipeline
    )


async def test_state_server_concurrent_acquire_and_stale_claim_are_fenced(tmp_path, recorder):
    await init_db(tmp_path / "state.db")
    service = TestServer(StateServer(credentials()).app)
    try:
        await service.start_server()
        address = str(service.make_url("/node/ws"))
        async with (
            NodeClient(address, "pc") as pc,
            NodeClient(address, "server") as server,
            NodeClient(address, "chat") as bot,
        ):
            await bot.request(RegisterNode(runtime_abi=2))
            assert next(node for node in (await bot.request(Status())).nodes if node.id == "chat").compatible
            await pc.request(RegisterNode(runtime_abi=2))
            with pytest.raises(NodeRequestError, match="incompatible"):
                await pc.request(Acquire())
            await pc.request(RegisterNode())
            grants = await asyncio.gather(pc.request(Acquire()), server.request(Acquire()))
            assert sum(result.lease is not None for result in grants) == 1
            lease = grants[0].lease
            assert lease is not None and lease.owner == "pc"
            await pc.request(Handoff(epoch=lease.epoch, target="server"))
            successor = (await server.request(Acquire())).lease
            assert successor is not None and successor.epoch > lease.epoch
            await bot.request(
                Receive(
                    message=IncomingMessage(
                        id="after-transfer", client_id="chat", conversation_id="master", text="接着聊吧。"
                    )
                )
            )
            with pytest.raises(NodeRequestError, match="lease"):
                await pc.request(Claim(epoch=lease.epoch))
            claim = (await server.request(Claim(epoch=successor.epoch))).claim
            assert claim is not None and claim.message.id == "after-transfer"
            recorder.record(
                "checked",
                invariant="real_state_server_grants_one_owner_and_rejects_stale_claim",
                previous_epoch=lease.epoch,
                current_epoch=successor.epoch,
            )
    finally:
        await service.close()
        await close_db()


async def test_slow_memory_refresh_cannot_overwrite_committed_controls(running_core, monkeypatch, recorder):
    core, state, bot, app = running_core
    app.scripted.add_route(when=lambda request: True, text="我会等你一起读诗。<timeout: 60s>", name="waiting")
    refreshing, resumed, renewed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    memory = core.memory()
    original_view, original_dispatch = memory.apply_view, state.dispatch
    renewals = 0

    async def apply_view(view):
        if any("等你一起读诗" in turn.content for turn in view.turns) and not refreshing.is_set():
            refreshing.set()
            await resumed.wait()
        await original_view(view)

    async def dispatch(node, request):
        nonlocal renewals
        response = await original_dispatch(node, request)
        if node.id == "pc" and isinstance(request, Renew) and refreshing.is_set():
            renewals += 1
            if renewals >= 2:
                renewed.set()
        return response

    monkeypatch.setattr(memory, "apply_view", apply_view)
    monkeypatch.setattr(state, "dispatch", dispatch)
    epoch = core.epoch()
    try:
        incoming = IncomingMessage(id="wait", client_id="chat", conversation_id="master", text="等我一下。")
        await bot.request(Receive(message=incoming))
        await asyncio.wait_for(refreshing.wait(), 10)
        await asyncio.wait_for(renewed.wait(), 5)
        saved = (await core.connection().request(LoadRuntime(epoch=core.epoch()))).runtime
        assert saved is not None and saved.timeout_set_at is not None
        assert core.epoch() == epoch
        resumed.set()
        async with asyncio.timeout(5):
            while core.muika is not None and core.muika.runtime_controls().timeout_set_at is None:
                await asyncio.sleep(0.05)
        assert core.muika is not None
        assert core.muika.runtime_controls().timeout_set_at == saved.timeout_set_at
        recorder.record(
            "checked",
            invariant="renewals_continue_without_overwriting_a_committed_reply_during_memory_refresh",
            epoch=epoch,
            renewals=renewals,
        )
    finally:
        resumed.set()
