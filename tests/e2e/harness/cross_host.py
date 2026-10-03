"""通过 TLS 验证 Windows Core 与 Linux Docker 状态服务的实际连接。"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from uuid import uuid4

os.environ.setdefault("MASTER_ID", "cross-host-master")
os.environ.setdefault("IPC_SECRET", "cross-host-unused-legacy-secret")

import pytest  # noqa: E402
from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402
from harness.core_app import CoreApp  # noqa: E402
from harness.scripted_llm import ScriptedTurn  # noqa: E402
from harness.trace import TraceRecorder  # noqa: E402

from muika.config import mas_config  # noqa: E402
from muika.database.db import close_db, init_db  # noqa: E402
from muika.ipc.bot_client import DurableBotClient  # noqa: E402
from muika.ipc.node_client import NodeClient  # noqa: E402
from muika.ipc.node_protocol import Status  # noqa: E402
from muika.node.auth import CredentialStore  # noqa: E402
from muika.node.config import ServerProfile, write_private_json  # noqa: E402
from muika.node.core_node import CoreNode  # noqa: E402
from muika.node.models import IncomingMessage  # noqa: E402


async def docker(*args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    output, _ = await process.communicate()
    if process.returncode:
        raise RuntimeError(output.decode(errors="replace"))
    return output.decode().strip()


def certificate(directory: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ip_address("127.0.0.1"))]), False
        )
        .sign(key, hashes.SHA256())
    )
    (directory / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="mas-node:review")
    parser.add_argument("--artifacts", type=Path, default=Path("tests/e2e/artifacts/cross_host"))
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    recorder = TraceRecorder(args.artifacts)
    name = "mas-cross-host-" + uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix="mas-cross-host-") as temporary:
        root = Path(temporary)
        state = root / "state"
        state.mkdir()
        workspace = root / "workspace"
        (workspace / "configs").mkdir(parents=True)
        (workspace / "configs/models.yml").write_text("main:\n  provider: _echo\n  default: true\n", encoding="utf-8")
        certificate(state)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        address = f"wss://127.0.0.1:{port}/node/ws"
        profile = ServerProfile(
            directory=Path("/state"),
            database=Path("/state/state.db"),
            public_address="wss://localhost:8766/node/ws",
            host="0.0.0.0",
            port=8766,
            certificate=Path("/state/cert.pem"),
            private_key=Path("/state/key.pem"),
            embedded_core=True,
            ca_file=Path("/state/cert.pem"),
            timezone="UTC",
        ).model_dump(mode="json")
        profile.update(
            directory="/state",
            database="/state/state.db",
            certificate="/state/cert.pem",
            private_key="/state/key.pem",
            ca_file="/state/cert.pem",
        )
        write_private_json(state / "server.json", profile)
        store = CredentialStore(state / "credentials.db")
        _, core_token = store.redeem(store.issue("windows", "core"))
        _, bot_token = store.redeem(store.issue("chat", "bot"))
        _, observer_token = store.redeem(store.issue("observer", "bot"))
        received = []

        async def deliver(message, resources):
            received.append(message)

        bot = DurableBotClient(
            address, bot_token, "chat", root / "bot", deliver, input_timeout=0, ca_file=state / "cert.pem"
        )
        jobs = []
        try:
            await docker(
                "run",
                "-d",
                "--name",
                name,
                "-p",
                f"127.0.0.1:{port}:8766",
                "--mount",
                f"type=bind,src={state},dst=/state",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace,readonly",
                "-e",
                "MASTER_ID=cross-host-master",
                "-e",
                "IPC_SECRET=cross-host-unused-secret",
                "--entrypoint",
                "python",
                args.image,
                "-m",
                "muika.node",
                "serve",
                "/state/server.json",
            )
            with pytest.MonkeyPatch.context() as monkeypatch:
                monkeypatch.setattr(mas_config, "data_dir", root / "windows")
                await init_db(root / "windows/device-audit.db")
                monkeypatch.setattr(mas_config, "persona_template", "Muika.md.jinja2")
                monkeypatch.setattr(mas_config, "agent_template", "Muika.agent.jinja2")
                CoreApp(monkeypatch, recorder, turns=[ScriptedTurn(text="我在另一台机器上，也还记得你。")])
                jobs.append(asyncio.create_task(bot.run()))
                await asyncio.wait_for(bot.connected.wait(), 30)
                async with NodeClient(address, observer_token, ca_file=state / "cert.pem") as observer:
                    async with asyncio.timeout(30):
                        while True:
                            status = await observer.request(Status())
                            if status.lease is not None and status.lease.owner == "server":
                                break
                            await asyncio.sleep(0.1)
                core = CoreNode(address, core_token, "windows", root / "windows", ca_file=state / "cert.pem")
                jobs.append(asyncio.create_task(core.run()))
                async with asyncio.timeout(30):
                    while core.client is None:
                        await asyncio.sleep(0.1)
                await bot.queue_input(
                    IncomingMessage(
                        id="handoff",
                        client_id="chat",
                        conversation_id="original",
                        kind="command",
                        text=".nodes handoff windows",
                    )
                )
                await asyncio.wait_for(core.ready.wait(), 30)
                await bot.queue_input(
                    IncomingMessage(id="cross-host", client_id="chat", conversation_id="original", text="能听到我吗？")
                )
                async with asyncio.timeout(30):
                    while not any(reply.kind == "send_message" for reply in received):
                        await asyncio.sleep(0.1)
                replies = [reply for reply in received if reply.kind == "send_message"]
                assert len(replies) == 1 and all(reply.conversation_id == "original" for reply in received)
                assert "另一台机器" in replies[0].text
                assert sum(reply.kind == "command_result" for reply in received) == 1
                assert core.muika is not None
                assert all(
                    abs((datetime.now() - turn.timestamp).total_seconds()) < 30
                    for turn in core.muika.memory.recent_turns
                )
                recorder.record(
                    "checked",
                    invariant="host_core_linux_state_real_tls_certificate_and_original_route",
                    host=sys.platform,
                    state="Linux Docker",
                    tls=True,
                    state_timezone="UTC",
                    supervised_server_core=True,
                    replies=1,
                )
        finally:
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            await bot.close()
            await close_db()
            log = await docker("logs", name)
            (args.artifacts / "state.log").write_text(log, encoding="utf-8")
            await docker("rm", "-f", name)
            recorder.write()
    print(json.dumps({"result": "passed", "artifact": str(args.artifacts.resolve())}))


if __name__ == "__main__":
    asyncio.run(main())
