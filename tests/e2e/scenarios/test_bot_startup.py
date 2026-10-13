"""独立 Bot 进程的适配器选择、启动失败和真实 IPC 建链场景。

失败边界：配置被逐字符加载、空配置静默启动、模块缺失被混同于内部依赖缺失、
非适配器模块进入注册，以及平台包安装后仍不能加载 MAS 或完成 IPC 鉴权。
"""

import asyncio
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

import pytest

from muika.ipc.server import CoreWsServer
from muika.utils.first_run import UserAgreement, load_agreement_content

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
async def bot_process(request, tmp_path, recorder, unused_tcp_port_factory):
    """用临时配置启动真实入口，保存输出并清理子进程。"""
    core_port = unused_tcp_port_factory()
    server = CoreWsServer(port=core_port, secret="startup-test-secret")
    await server.start()
    UserAgreement(tmp_path / "data" / "user_agreement.json").accept(load_agreement_content().updated)

    adapter_config = request.param
    if isinstance(adapter_config, tuple):
        module, source = adapter_config
        adapter_config = json.dumps([module])
        if source is not None:
            (tmp_path / f"{module}.py").write_text(source, encoding="utf-8")
    settings = [
        "DRIVER=~fastapi+~httpx+~websockets",
        "HOST=127.0.0.1",
        f"PORT={unused_tcp_port_factory()}",
        "MASTER_ID=startup-test-master",
        'SUPERUSERS=["startup-test-master"]',
        "IPC_SECRET=startup-test-secret",
        f"CORE_WS_URL=ws://127.0.0.1:{core_port}/ws",
        "CLIENT_NAME=startup-test-bot",
    ]
    if adapter_config is not None:
        settings.append(f"ENABLE_ADAPTERS={adapter_config}")
    (tmp_path / ".env").write_text("\n".join(settings), encoding="utf-8")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "HOME", "USERPROFILE", "PATHEXT"}
    }
    environment.update(PYTHONPATH=os.pathsep.join((str(tmp_path), str(ROOT))), PYTHONIOENCODING="utf-8")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-u",
        str(ROOT / "bot.py"),
        cwd=tmp_path,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output = asyncio.create_task(process.communicate())
    try:
        yield process, output, server
    finally:
        if process.returncode is None:
            process.terminate()
        stdout, _ = await asyncio.wait_for(output, timeout=15)
        recorder.record("bot_process", returncode=process.returncode, output=stdout.decode("utf-8", errors="replace"))
        await server.stop()


@pytest.mark.parametrize(
    "bot_process",
    ['"nonebot.adapters.onebot.v11"', "[]", "[1]", '[""]', '["   "]', '{"adapter":"onebot"}'],
    indirect=True,
)
async def test_invalid_adapter_config_stops_startup(bot_process):
    """错误配置在加载插件前退出，并指出配置格式。"""
    process, output, server = bot_process
    stdout, _ = await asyncio.wait_for(asyncio.shield(output), timeout=20)
    assert process.returncode != 0
    assert "ENABLE_ADAPTERS must be a non-empty list of non-empty module names" in stdout.decode("utf-8")
    assert not server.has_connection


@pytest.mark.parametrize(
    "bot_process,diagnostic",
    [
        (("missing_platform.adapter", None), "is not installed"),
        (
            ("broken_platform", "import missing_platform_dependency\n"),
            "missing dependency 'missing_platform_dependency'",
        ),
        (
            ("bad_import_platform", "raise ImportError('adapter initialization failed')\n"),
            "adapter initialization failed",
        ),
        (("wrong_platform", "Adapter = object\n"), "must export an Adapter subclass"),
        (("no_adapter_platform", "value = 1\n"), "must export an Adapter subclass"),
    ],
    indirect=["bot_process"],
)
async def test_adapter_import_errors_identify_cause(bot_process, request, diagnostic):
    """实际导入失败保留模块名和具体原因，不进入 IPC。"""
    process, output, server = bot_process
    stdout, _ = await asyncio.wait_for(asyncio.shield(output), timeout=20)
    text = stdout.decode("utf-8")
    assert process.returncode != 0
    assert request.node.callspec.params["bot_process"][0] in text
    assert diagnostic in text
    assert not server.has_connection


@pytest.mark.parametrize(
    "bot_process,adapter_name",
    [
        (None, "OneBot V11"),
        pytest.param(
            '["nonebot.adapters.telegram"]',
            "Telegram",
            marks=pytest.mark.skipif(
                importlib.util.find_spec("nonebot.adapters.telegram") is None,
                reason="Install nonebot-adapter-telegram to run the Telegram startup scenario",
            ),
        ),
    ],
    indirect=["bot_process"],
)
async def test_selected_adapter_loads_mas_and_connects_core(bot_process, adapter_name):
    """默认 OneBot 或显式 Telegram 加载 MAS 并完成真实 Core 鉴权。"""
    process, output, server = bot_process
    try:
        for _ in range(300):
            if server.has_connection or process.returncode is not None:
                break
            await asyncio.sleep(0.1)
        assert server.has_connection
    finally:
        if process.returncode is None:
            process.terminate()
    stdout, _ = await asyncio.wait_for(asyncio.shield(output), timeout=15)
    text = re.sub(r"\x1b\[[0-9;]*m", "", stdout.decode("utf-8"))
    assert adapter_name in text
    assert 'Succeeded to load plugin "muika_bot"' in text
    assert "Failed to import" not in text
