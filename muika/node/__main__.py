"""部署固定入口、配对设备，并运行 Core 或执行角色。"""

import argparse
import asyncio
import json
import os
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from tzlocal import get_localzone_name

from muika.config import mas_config
from muika.database.db import close_db, database_path, get_session, init_db
from muika.database.orm_models import RuntimeStateORM
from muika.ipc.bot_client import BotSpool
from muika.ipc.node_client import NodeClient
from muika.ipc.state_server import StateServer
from muika.node.auth import CredentialStore
from muika.node.bundle import CognitiveBundle
from muika.node.config import NodeProfile, ServerProfile, write_private_json
from muika.node.core_node import CoreNode
from muika.node.executor_node import ExecutorNode
from muika.node.plugins import load_node_plugins, unload_node_plugins
from muika.node.service_lock import StateServiceLock
from muika.node.transfer import export_snapshot, import_snapshot
from muika.plugin.mcp import cleanup_servers, initialize_servers
from muika.utils.logger import logger

if sys.platform == "win32":
    SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    SUBPROCESS_FLAGS = 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="MAS 常驻入口与设备配对")
    sub = cli.add_subparsers(dest="command", required=True)
    initialize = sub.add_parser("init-server", help="创建常驻部署配置，保留现有用户文件")
    initialize.add_argument("directory", type=Path)
    initialize.add_argument("--address", default="ws://127.0.0.1:8766/node/ws")
    initialize.add_argument("--host", default="127.0.0.1")
    initialize.add_argument("--port", type=int, default=8766)
    initialize.add_argument("--certificate", type=Path)
    initialize.add_argument("--private-key", type=Path)
    initialize.add_argument("--ca-file", type=Path)
    for name in ("serve", "pair", "revoke"):
        action = sub.add_parser(name)
        action.add_argument("profile", type=Path)
        if name in {"pair", "revoke"}:
            action.add_argument("id")
        if name == "pair":
            action.add_argument("--role", choices=["core", "executor", "bot"], default="core")
            action.add_argument("--priority", type=int, default=100)
    join = sub.add_parser("join", help="使用配对码保存设备配置")
    join.add_argument("address")
    join.add_argument("code")
    join.add_argument("directory", type=Path)
    join.add_argument("--ca-file", type=Path)
    for name in ("run", "status"):
        action = sub.add_parser(name)
        action.add_argument("profile", type=Path)
        if name == "run":
            action.add_argument("--supervised", action="store_true", help=argparse.SUPPRESS)
    export = sub.add_parser("export", help="停止原单机 Core 后导出数据库和资源快照")
    export.add_argument("database", type=Path)
    export.add_argument("archive", type=Path)
    restore = sub.add_parser("import", help="核对快照并导入空的常驻部署")
    restore.add_argument("archive", type=Path)
    restore.add_argument("profile", type=Path)
    restore.add_argument(
        "--accept-rollback-window", action="store_true", help="确认旧实例已停止；回滚原库会丢失启用常驻模式后的新数据"
    )
    sub.add_parser("publish-config", help="停止状态服务后，从工作目录发布认知配置").add_argument("profile", type=Path)
    delivery = sub.add_parser("resolve-delivery", help="停止对应 Bot 后核对不确定投递")
    delivery.add_argument("profile", type=Path)
    delivery.add_argument("id")
    delivery.add_argument("--outcome", choices=["delivered", "not-delivered"], required=True)
    return cli


async def serve(profile: ServerProfile) -> None:
    """承载状态入口，并监督使用独立进程的服务器 Core。"""
    if sys.platform == "win32":
        if profile.timezone != get_localzone_name():
            raise ValueError(
                "State service timezone differs from Windows. Use a matching system timezone or a Linux State service."
            )
    else:
        os.environ["TZ"] = profile.timezone
        time.tzset()
    mas_config.data_dir = profile.directory
    await init_db(profile.database)
    runner: web.AppRunner | None = None
    core_job: asyncio.Task[None] | None = None
    try:
        store = CredentialStore(profile.directory / "credentials.db")
        embedded_path = profile.directory / "server-core.json"
        if profile.embedded_core:
            if embedded_path.exists():
                embedded = NodeProfile.read(embedded_path)
                if embedded.id != profile.id or embedded.role != "core":
                    raise ValueError("Server Core identity differs from its state service configuration.")
                embedded.address, embedded.ca_file = profile.public_address, profile.ca_file
            else:
                credential, token = store.redeem(store.issue(profile.id, "core", 1000))
                embedded = NodeProfile(
                    id=credential.id,
                    role="core",
                    address=profile.public_address,
                    token=token,
                    directory=profile.directory / "core",
                    ca_file=profile.ca_file,
                )
            write_private_json(embedded_path, embedded)
        async with get_session() as db:
            saved = await db.get(RuntimeStateORM, 2)
            initial = (
                CognitiveBundle.model_validate_json(saved.payload) if saved else CognitiveBundle.capture(Path.cwd())
            )
        initial.validate_configuration()
        service = StateServer([], credential_store=store, initial_bundle=initial)
        runner = web.AppRunner(service.app)
        await runner.setup()
        context = None
        if profile.certificate and profile.private_key:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(profile.certificate, profile.private_key)
        await web.TCPSite(runner, profile.host, profile.port, ssl_context=context).start()
        logger.info(f"[Node] State service ready: {profile.public_address}")
        if profile.embedded_core:
            core_job = asyncio.create_task(supervise_core(embedded_path))
        await asyncio.Event().wait()
    finally:
        if core_job is not None:
            core_job.cancel()
            await asyncio.gather(core_job, return_exceptions=True)
        if runner is not None:
            await runner.cleanup()
        await close_db()


async def supervise_core(path: Path) -> None:
    """重启退出的服务器 Core，不共享状态服务的配置和数据库连接。"""
    while True:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "muika.node",
            "run",
            str(path),
            "--supervised",
            stdin=asyncio.subprocess.PIPE,
            creationflags=SUBPROCESS_FLAGS,
        )
        try:
            code = await process.wait()
            logger.warning(f"[Node] Server Core exited: {code}; restarting.")
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
        await asyncio.sleep(2)


async def wait_for_parent() -> None:
    """父进程管道关闭后停止子节点，初始化失败不等待阻塞读取线程。"""
    loop = asyncio.get_running_loop()
    finished = loop.create_future()

    def complete() -> None:
        if not finished.done():
            finished.set_result(None)

    def read() -> None:
        while os.read(sys.stdin.fileno(), 65536):
            pass
        try:
            loop.call_soon_threadsafe(complete)
        except RuntimeError:
            pass

    threading.Thread(target=read, daemon=True).start()
    await finished


async def run(profile: NodeProfile) -> None:
    """运行配对角色，Bot 使用其自己的适配器启动入口。"""
    if profile.role == "bot":
        raise ValueError("Bot 配置已保存。请设置 NODE_PROFILE 后启动原有 Bot 服务。")
    mas_config.data_dir = profile.directory
    device_plugins: list[str] = []
    owns_database = False
    try:
        await initialize_servers()
        device_plugins = load_node_plugins(profile.plugins, "device")
        try:
            database_path()
        except RuntimeError:
            await init_db(profile.directory / "device-audit.db")
            owns_database = True
        if profile.role == "core":
            await CoreNode(
                profile.address,
                profile.token,
                profile.id,
                profile.directory,
                lease_seconds=profile.lease_seconds,
                ca_file=profile.ca_file,
                plugins=profile.plugins,
            ).run()
        else:
            await ExecutorNode(
                profile.address, profile.token, profile.id, profile.directory, ca_file=profile.ca_file
            ).run()
    finally:
        await cleanup_servers()
        unload_node_plugins(device_plugins)
        if owns_database:
            await close_db()


async def join_device(address_text: str, code: str, directory: Path, ca_file: Path | None) -> None:
    """用一次性配对码取得身份，并保存本机连接配置。"""
    directory = directory.resolve()
    path = directory / "node.json"
    if path.exists():
        raise ValueError("设备配置已存在；请先撤销旧凭据，再使用新的目录配对。")
    # 复用连接地址验证，拒绝向远程明文入口发送配对码。
    NodeClient(address_text, "pairing", ca_file=ca_file)
    address = urlsplit(address_text)
    scheme = "https" if address.scheme in {"https", "wss"} else "http"
    context = ssl.create_default_context(cafile=str(ca_file)) if ca_file else True
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as session:
        async with session.post(f"{scheme}://{address.netloc}/node/pair", data=code) as response:
            if response.status != 200:
                raise ValueError(await response.text())
            identity = await response.json()
    write_private_json(path, NodeProfile(**identity, address=address_text, directory=directory, ca_file=ca_file))
    print(f"配对完成: {identity['id']} ({identity['role']})。配置保存在 {path}。")


async def publish_configuration(profile_path: Path) -> None:
    """在服务停用时验证并发布新的认知配置。"""
    server_profile = ServerProfile.read(profile_path)
    if not server_profile.database.is_file():
        raise ValueError("请先初始化状态服务数据库。")
    bundle = CognitiveBundle.capture(Path.cwd())
    bundle.validate_configuration()
    lock = StateServiceLock(server_profile.database)
    lock.acquire()
    try:
        with closing(sqlite3.connect(server_profile.database)) as db, db:
            changed = db.execute("UPDATE runtime_state SET payload=? WHERE id=2", (bundle.model_dump_json(),))
            if changed.rowcount != 1:
                raise ValueError("请先启动状态服务，完成常驻部署初始化。")
    finally:
        lock.close()
    print("已发布认知配置。重新启动状态服务和候选 Core 后生效。")


async def main() -> None:
    args = parser().parse_args()
    if args.command == "init-server":
        directory = args.directory.resolve()
        path = directory / "server.json"
        if path.exists():
            raise ValueError("配置已存在，请编辑现有 server.json。")
        profile = ServerProfile(
            public_address=args.address,
            host=args.host,
            port=args.port,
            directory=directory,
            database=directory / "muika.db",
            certificate=args.certificate,
            private_key=args.private_key,
            ca_file=args.ca_file,
        )
        write_private_json(path, profile)
        print(f"已创建 {path}。启动前请在工作目录配置 models.yml 和 .env。")
    elif args.command == "serve":
        await serve(ServerProfile.read(args.profile))
    elif args.command in {"pair", "revoke"}:
        profile = ServerProfile.read(args.profile)
        store = CredentialStore(profile.directory / "credentials.db")
        if args.command == "pair":
            print(store.issue(args.id, args.role, args.priority))
            print("配对码 10 分钟内有效，只能使用一次。请通过可信渠道交给目标设备。")
        else:
            store.revoke(args.id)
            print(f"已撤销设备 {args.id}。")
    elif args.command == "join":
        await join_device(args.address, args.code, args.directory, args.ca_file)
    elif args.command == "export":
        report = await export_snapshot(args.database, args.archive, Path.cwd())
        print(f"已导出 {len(report['tables'])} 张表和资源快照。原库保持原版本。")
    elif args.command == "import":
        if not args.accept_rollback_window:
            raise ValueError(
                "启用前请停止原 Core 并保留原库。回滚原库不会保留常驻模式的新对话；确认后加 --accept-rollback-window。"
            )
        imported_profile = ServerProfile.read(args.profile)
        report = import_snapshot(args.archive, imported_profile)
        write_private_json(args.profile, imported_profile)
        print(f"已核对并导入 {len(report['tables'])} 张表。导入报告已保存；请保持原实例停用。")
    elif args.command == "resolve-delivery":
        node_profile = NodeProfile.read(args.profile)
        if node_profile.role != "bot":
            raise ValueError("投递核对只适用于 Bot 配置。")
        spool = BotSpool(node_profile.directory)
        try:
            spool.resolve_delivery(args.id, args.outcome == "delivered")
        finally:
            spool.close()
        print("已保存核对结果。重新启动 Bot 后继续确认或投递。")
    elif args.command == "publish-config":
        await publish_configuration(args.profile)
    else:
        node_profile = NodeProfile.read(args.profile)
        if args.command == "run":
            if args.supervised:
                work = asyncio.create_task(run(node_profile))
                parent = asyncio.create_task(wait_for_parent())
                try:
                    done, _ = await asyncio.wait([work, parent], return_when=asyncio.FIRST_COMPLETED)
                    if work in done:
                        await work
                finally:
                    work.cancel()
                    parent.cancel()
                    await asyncio.gather(work, parent, return_exceptions=True)
            else:
                await run(node_profile)
        else:
            NodeClient(node_profile.address, node_profile.token, ca_file=node_profile.ca_file)
            address = urlsplit(node_profile.address)
            scheme = "https" if address.scheme in {"https", "wss"} else "http"
            context = ssl.create_default_context(cafile=str(node_profile.ca_file)) if node_profile.ca_file else True
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=context)) as session:
                async with session.get(
                    f"{scheme}://{address.netloc}/node/status",
                    headers={"Authorization": f"Bearer {node_profile.token}"},
                ) as response:
                    response.raise_for_status()
                    print(json.dumps(await response.json(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except (ValueError, RuntimeError, OSError, sqlite3.DatabaseError, aiohttp.ClientError) as error:
        raise SystemExit(f"无法完成操作: {error}") from error
