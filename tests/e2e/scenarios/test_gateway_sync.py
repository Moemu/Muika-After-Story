"""真实 Gateway 进程与独立 WebSocket：活动权、持久历史和 Bot 边界。"""

import asyncio
import json
import os
import socket
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import aiohttp
import pytest

from tests.e2e.harness.process import MemoryProcess

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def gateway_process(tmp_path, recorder):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    log = (tmp_path / "gateway.log").open("wb")

    async def launch():
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "muika.ipc.gateway",
            "--port",
            str(port),
            "--directory",
            str(tmp_path / "gateway"),
            cwd=ROOT,
            env=dict(os.environ, LOGURU_LEVEL="ERROR", PYTHONIOENCODING="utf-8"),
            stdout=log,
            stderr=log,
        )

    process = await launch()
    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession(headers={"X-Auth-Token": "test-ipc-secret"}) as session:
        try:
            for _ in range(100):
                if process.returncode is not None:
                    raise AssertionError((tmp_path / "gateway.log").read_text(errors="replace"))
                try:
                    async with session.get(base + "/health") as response:
                        if response.status == 200:
                            break
                except aiohttp.ClientError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("Gateway did not start")

            async def restart():
                nonlocal process
                if process.returncode is None:
                    process.kill()
                await process.wait()
                process = await launch()
                for _ in range(100):
                    try:
                        async with session.get(base + "/health") as response:
                            if response.status == 200:
                                return
                    except aiohttp.ClientError:
                        pass
                    await asyncio.sleep(0.05)
                raise AssertionError("Gateway did not restart")

            yield session, base, recorder, process, restart
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()
            log.close()
            trace_path = recorder.write()
            assert trace_path is not None
            (trace_path.parent / "gateway.log").write_bytes((tmp_path / "gateway.log").read_bytes())


async def connect_core(session, base, name, *, protocol="1"):
    return await session.ws_connect(
        base + "/cores",
        headers={
            "X-Core-Name": name,
            "X-Node-ID": name,
            "X-Sync-Protocol": protocol,
        },
    )


async def packet(ws, kind, recorder):
    while True:
        result = await asyncio.wait_for(ws.receive_json(), 20)
        recorder.record("gateway_packet", packet=result)
        if result["kind"] == kind:
            return result


async def bot_output(ws, recorder):
    while True:
        result = await asyncio.wait_for(ws.receive_json(), 10)
        recorder.record("bot_packet", packet=result)
        if result["type"] == "send_message":
            return result


async def ready(ws, recorder):
    await ws.send_json({"kind": "history", "after": 0})
    await packet(ws, "history", recorder)
    await ws.send_json({"kind": "ready"})
    return await packet(ws, "role", recorder)


async def test_chat_attachment_survives_original_temp_file_and_checks_access(gateway_process, tmp_path):
    from io import BytesIO

    from muika.ipc.attachments import AttachmentTransfer
    from muika.models import Resource

    session, base, recorder = gateway_process[:3]
    transfer = AttachmentTransfer(base + "/ws", "test-ipc-secret", tmp_path / "bot-cache")
    resource = Resource("file", raw=BytesIO(b"only lived in a bot event"), mimetype="text/plain")
    uploaded = await transfer.upload(resource)
    other = AttachmentTransfer(base + "/cores", "test-ipc-secret", tmp_path / "pc-cache")
    downloaded = await other.download(Resource(**uploaded))
    assert Path(downloaded.path).read_bytes() == b"only lived in a bot event"
    assert downloaded.mimetype == "text/plain"
    recorder.record("attachment_roundtrip", uploaded=uploaded, downloaded=downloaded.to_dict())
    async with session.get(base + uploaded["url"], headers={"X-Auth-Token": "wrong"}) as response:
        assert response.status == 401
    async with session.put(base + "/attachments/" + "0" * 64, data=b"wrong hash") as response:
        assert response.status == 400
    async with session.get(base + "/attachments/" + "a" * 64) as response:
        assert response.status == 404


async def test_attachments_use_each_devices_gateway_address(gateway_process, node_process):
    from muika.ipc.attachments import AttachmentTransfer
    from muika.models import Resource

    session, base, recorder = gateway_process[:3]
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base.replace("127.0.0.1", "localhost"))
    await wait_node(server, active=False, connected=True)
    attachment = await AttachmentTransfer(base + "/ws", "test-ipc-secret", Path("bot-cache")).upload(
        Resource("file", raw=b"shared chat file", mimetype="text/plain")
    )
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "attachments"})
    await bot.send_json({"type": "user_message", "id": "file", "message": "这是聊天附件。", "resources": [attachment]})
    await bot_output(bot, recorder)
    await pc.command(action="handoff", target="server")
    await wait_node(server, active=True)
    assert (await server.command(action="status"))["turns"].count("这是聊天附件。") == 1


async def test_gateway_handoff_fences_old_output_without_reply_pairing(gateway_process):
    session, base, recorder = gateway_process[:3]
    pc = await connect_core(session, base, "pc")
    role = await ready(pc, recorder)
    assert role["active"] == "pc"
    server = await connect_core(session, base, "server")
    await ready(server, recorder)
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "telegram"})
    await pc.send_json({"kind": "handoff", "target": "server", "epoch": role["epoch"]})
    transferred = await packet(server, "role", recorder)
    while transferred["active"] != "server":
        transferred = await packet(server, "role", recorder)
    await pc.send_json(
        {"kind": "output", "epoch": role["epoch"], "message": {"type": "send_message", "content": "过期输出"}}
    )
    assert (await packet(pc, "error", recorder))["detail"] == "Inactive Core"
    await server.send_json(
        {
            "kind": "output",
            "epoch": transferred["epoch"],
            "message": {"type": "send_message", "content": "我自己想说的话"},
        }
    )
    output = await asyncio.wait_for(bot.receive_json(), 5)
    recorder.record("bot_output", message=output)
    assert output["content"] == "我自己想说的话"
    assert "source_message_id" not in output


async def test_gateway_queues_input_until_core_available(gateway_process):
    session, base, recorder = gateway_process[:3]
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "qq"})
    await bot.send_json({"type": "user_message", "id": "input-1", "message": "你在吗？"})
    pc = await connect_core(session, base, "pc")
    await ready(pc, recorder)
    incoming = await packet(pc, "input", recorder)
    assert incoming["message"]["id"] == "input-1"
    assert incoming["adapter"] == "qq"


async def test_gateway_lease_expires_with_connected_socket(gateway_process):
    session, base, recorder = gateway_process[:3]
    pc, server = await connect_core(session, base, "pc"), await connect_core(session, base, "server")
    await ready(pc, recorder)
    await ready(server, recorder)

    async def heartbeat():
        while True:
            await server.send_json({"kind": "heartbeat"})
            await asyncio.sleep(1)

    pulses = asyncio.create_task(heartbeat())
    try:
        role = await packet(server, "role", recorder)
        while role["active"] != "server":
            role = await packet(server, "role", recorder)
        assert not pc.closed
    finally:
        pulses.cancel()
        await asyncio.gather(pulses, return_exceptions=True)


async def test_gateway_history_is_not_delivery_and_protocol_is_checked(gateway_process):
    session, base, recorder = gateway_process[:3]
    rejected = await connect_core(session, base, "old", protocol="0")
    reason = await rejected.receive_json()
    assert reason["kind"] == "error" and "protocol" in reason["detail"].lower()
    pc = await connect_core(session, base, "pc")
    await ready(pc, recorder)
    activity = {"id": "silent-state", "origin": "pc", "experiences": []}
    await pc.send_json({"kind": "publish", "activities": [activity]})
    await packet(pc, "history", recorder)
    await pc.send_json({"kind": "publish", "activities": [activity]})
    await packet(pc, "history", recorder)
    server = await connect_core(session, base, "server")
    await server.send_json({"kind": "history", "after": 0})
    history = await packet(server, "history", recorder)
    assert len(history["entries"]) == 1
    assert history["entries"][0]["activity"]["id"] == "silent-state"


@pytest.fixture
async def node_process(tmp_path, recorder):
    nodes = []

    async def start(name, base, *, fallback=False):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            "tests.e2e.harness.node_process",
            str(tmp_path / name),
            name,
            base.replace("http:", "ws:") + "/cores",
            str(port),
            str(int(fallback)),
            cwd=ROOT,
            env=dict(os.environ, LOGURU_LEVEL="ERROR", PYTHONIOENCODING="utf-8"),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        node = MemoryProcess(process, recorder, name)
        nodes.append(node)
        return node, port

    yield start
    for node in nodes:
        await node.close()


async def wait_node(node, **expected):
    for _ in range(120):
        state = await node.command(action="status")
        synchronized = not expected.get("connected") or state.get("ready", True)
        if synchronized and all(state[key] == value for key, value in expected.items()):
            return state
        await asyncio.sleep(0.1)
    raise AssertionError(f"Node did not reach {expected}: {state}")


async def test_reminder_survives_handoff_without_standby_execution(gateway_process, node_process):
    session, base, recorder = gateway_process[:3]
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    await wait_node(server, active=False, connected=True)
    await pc.command(action="remind", text="提醒喝水", delay=3)
    await pc.command(action="handoff", target="server")
    await wait_node(server, active=True)
    await wait_node(server, reminders=1)
    assert (await pc.command(action="status"))["reminders"] == 0

    await server.command(action="handoff", target="pc")
    await wait_node(pc, active=True)
    await asyncio.sleep(1)
    assert (await pc.command(action="status"))["reminders"] == 0


async def test_bot_falls_back_and_returns_to_primary(gateway_process, node_process, tmp_path):
    session, base, recorder, gateway, restart = gateway_process
    pc, port = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    primary, fallback = base + "/ws", f"http://127.0.0.1:{port}/ws"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "tests.e2e.harness.bot_process",
        primary,
        fallback,
        str(tmp_path / "bot"),
        cwd=ROOT,
        env=dict(os.environ, LOGURU_LEVEL="ERROR", PYTHONIOENCODING="utf-8"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    bot = MemoryProcess(process, recorder, "bot")
    try:
        await wait_node(bot, connected=True, endpoint=primary)
        gateway.kill()
        await gateway.wait()
        await wait_node(pc, active=True, connected=False)
        await wait_node(bot, connected=True, endpoint=fallback)
        await bot.command(action="send", text="本地继续聊天。")
        for _ in range(100):
            state = await bot.command(action="status")
            if state["outputs"]:
                break
            await asyncio.sleep(0.1)
        assert state["outputs"][0]["content"] == "我记得我们的散步约定。"
        await restart()
        await wait_node(pc, active=True, connected=True)
        await wait_node(bot, connected=True, endpoint=primary)
    finally:
        await bot.close()


async def test_bot_keeps_reconnecting_without_an_alternative(tmp_path, recorder):
    from aiohttp import web

    blocked = False
    connections = []

    async def endpoint(request):
        if blocked:
            raise web.HTTPServiceUnavailable()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connections.append(ws)
        async for message in ws:
            pass
        return ws

    app = web.Application()
    app.add_routes([web.get("/ws", endpoint)])
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    primary = f"http://127.0.0.1:{port}/ws"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        "-m",
        "tests.e2e.harness.bot_process",
        primary,
        "",
        str(tmp_path / "bot"),
        cwd=ROOT,
        env=dict(os.environ, LOGURU_LEVEL="ERROR", PYTHONIOENCODING="utf-8"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    bot = MemoryProcess(process, recorder, "bot")
    try:
        await wait_node(bot, connected=True, endpoint=primary)
        blocked = True
        await connections[0].close()
        await wait_node(bot, connected=False)
        await asyncio.sleep(1)
        blocked = False
        await wait_node(bot, connected=True, endpoint=primary)
    finally:
        await bot.close()
        await runner.cleanup()


async def test_network_partition_merges_both_live_histories(gateway_process, node_process):
    from aiohttp import web

    session, base, recorder = gateway_process[:3]
    blocked = False
    connections = []

    async def proxy(request):
        if blocked:
            raise web.HTTPServiceUnavailable()
        client = web.WebSocketResponse()
        await client.prepare(request)
        upstream = await session.ws_connect(base + "/cores", headers=dict(request.headers))
        connections.append(client)

        async def relay(source, target):
            async for message in source:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await target.send_str(message.data)

        relays = [asyncio.create_task(relay(client, upstream)), asyncio.create_task(relay(upstream, client))]
        try:
            await asyncio.wait(relays, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in relays:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
            await upstream.close()
            await client.close()
        return client

    app = web.Application()
    app.add_routes([web.get("/cores", proxy)])
    runner = web.AppRunner(app)
    await runner.setup()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        pc, local_port = await node_process("pc", f"http://127.0.0.1:{port}", fallback=True)
        await wait_node(pc, active=True, connected=True)
        server, _ = await node_process("server", base)
        await wait_node(server, active=False, connected=True)
        blocked = True
        await connections[0].close()
        await wait_node(pc, active=True, connected=False)
        await wait_node(server, active=True, connected=True)
        online = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "remote"})
        local = await session.ws_connect(f"http://127.0.0.1:{local_port}/ws", headers={"X-Client-Name": "local"})
        await online.send_json({"type": "user_message", "id": "remote-branch", "message": "远端留下的一段经历。"})
        await bot_output(online, recorder)
        await local.send_json({"type": "user_message", "id": "local-branch", "message": "本地留下的一段经历。"})
        await bot_output(local, recorder)
        await pc.command(action="mood", value="前台的安心")
        blocked = False
        await wait_node(pc, active=True, connected=True)
        await wait_node(server, active=False, mood="前台的安心")
        for node in (pc, server):
            state = await node.command(action="status")
            assert state["turns"].count("远端留下的一段经历。") == 1
            assert state["turns"].count("本地留下的一段经历。") == 1
            assert state["conversations"] == 1
    finally:
        await runner.cleanup()


async def test_real_core_shutdown_takeover_retains_memory(gateway_process, node_process):
    session, base, recorder = gateway_process[:3]
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    state = await wait_node(server, active=False, connected=True)
    assert state["model_calls"] == 0
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "test"})
    await bot.send_json({"type": "user_message", "id": "first", "message": "记住我们的散步约定。"})
    output = await asyncio.wait_for(bot.receive_json(), 10)
    assert output["content"] == "我记得我们的散步约定。"
    await wait_node(server, mood="期待散步")
    pc.process.kill()
    await pc.process.wait()
    await wait_node(server, active=True)
    await bot.send_json({"type": "user_message", "id": "second", "message": "接着陪我聊聊。"})
    assert (await bot_output(bot, recorder))["content"] == "我记得我们的散步约定。"
    restored = await server.command(action="status")
    assert restored["turns"].count("记住我们的散步约定。") == 1


async def test_pc_becomes_primary_after_server_started_first(gateway_process, node_process):
    session, base, recorder = gateway_process[:3]
    server, _ = await node_process("server", base)
    await wait_node(server, active=True, connected=True)
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    await wait_node(server, active=False, connected=True)


async def test_cold_sync_large_existing_history(gateway_process, node_process, memory_process, tmp_path):
    session, base, recorder = gateway_process[:3]
    saved = await memory_process("pc")
    await saved.command(action="status")
    await saved.close()
    with sqlite3.connect(tmp_path / "pc" / "muika.db") as db:
        snapshot = json.loads(db.execute("SELECT payload FROM memory_runtime").fetchone()[0])
        db.execute("DELETE FROM sync_event")
        db.execute("DELETE FROM sync_reference")
        db.executemany(
            "INSERT INTO experience(session_id,kind,content,occurred_at,resources) VALUES(?,?,?,?,?)",
            [
                (snapshot["session"]["session_id"], "note", "旧记忆" * 500, datetime.now().isoformat(), "[]")
                for _ in range(1200)
            ],
        )
    started = time.monotonic()
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    await wait_node(server, active=False, connected=True)
    with sqlite3.connect(tmp_path / "server" / "muika.db") as db:
        copied = db.execute("SELECT count(*) FROM experience WHERE content=?", ("旧记忆" * 500,)).fetchone()[0]
    assert copied == 1200
    assert (await server.command(action="status"))["conversations"] == 0
    recorder.record("cold_sync_measurement", experiences=copied, elapsed_seconds=time.monotonic() - started)


async def test_gateway_down_activates_only_designated_local_core(gateway_process, node_process):
    session, base, recorder, gateway = gateway_process[:4]
    pc, port = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    await wait_node(server, active=False, connected=True)
    gateway.kill()
    await gateway.wait()
    await wait_node(pc, active=True, connected=False)
    inactive = await wait_node(server, active=False, connected=False)
    assert inactive["model_calls"] == 0
    bot = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={"X-Client-Name": "local"})
    await bot.send_json({"type": "user_message", "id": "offline", "message": "离线也陪我聊聊。"})
    assert (await bot_output(bot, recorder))["content"] == "我记得我们的散步约定。"


async def test_reconnect_keeps_foreground_feeling_without_replaying_output(gateway_process, node_process):
    session, base, recorder, gateway, restart = gateway_process
    pc, port = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    await wait_node(server, active=False, connected=True)
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "remote"})
    await bot.send_json({"type": "user_message", "id": "before", "message": "恢复前的对话。"})
    await bot_output(bot, recorder)
    gateway.kill()
    await gateway.wait()
    await wait_node(pc, active=True, connected=False)
    local = await session.ws_connect(f"http://127.0.0.1:{port}/ws", headers={"X-Client-Name": "local"})
    await local.send_json({"type": "user_message", "id": "offline", "message": "失联时的对话。"})
    await bot_output(local, recorder)
    await pc.command(action="mood", value="离线时留下的感受")
    conversations = (await pc.command(action="status"))["conversations"]
    await restart()
    restored = await wait_node(pc, active=True, connected=True, mood="离线时留下的感受")
    await wait_node(server, active=False, connected=True, mood="离线时留下的感受")
    assert restored["turns"].count("失联时的对话。") == 1
    assert restored["conversations"] == conversations
    remote = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "remote"})
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(remote.receive_json(), 0.5)


async def test_nodes_command_and_autonomous_handoff_tool(gateway_process, node_process):
    session, base, recorder = gateway_process[:3]
    pc, _ = await node_process("pc", base, fallback=True)
    await wait_node(pc, active=True, connected=True)
    server, _ = await node_process("server", base)
    await wait_node(server, active=False, connected=True)
    bot = await session.ws_connect(base + "/ws", headers={"X-Client-Name": "commands"})
    await bot.send_json({"type": "command", "id": "list", "raw": ".nodes list"})
    result = await asyncio.wait_for(bot.receive_json(), 5)
    recorder.record("command_output", message=result)
    assert result["type"] == "command_result" and "server" in result["content"]
    requested = await pc.command(action="handoff", target="server")
    assert requested["tool_result"]["is_error"] is False
    assert "requested" in requested["tool_result"]["text"].lower()
    await wait_node(server, active=True)
    await wait_node(pc, active=False)
    await bot.send_json({"type": "command", "id": "return", "raw": ".nodes handoff pc"})
    await wait_node(pc, active=True)
