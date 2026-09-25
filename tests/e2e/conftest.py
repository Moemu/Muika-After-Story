"""E2E 场景共享夹具：运行轨迹记录器与 CoreApp 工厂。"""

import re
from pathlib import Path

import pytest
from harness import CoreApp, TraceRecorder

ARTIFACTS_ROOT = Path(__file__).parent / "artifacts"


@pytest.fixture
def recorder(request):
    """为每个场景生成独立轨迹，测试结束后写入 ``artifacts/<场景名>/trace.jsonl``。"""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.name)
    rec = TraceRecorder(ARTIFACTS_ROOT / name)
    yield rec
    rec.write()


@pytest.fixture
async def core_app_factory(monkeypatch, recorder):
    """提供 CoreApp 工厂；测试结束时统一停止所有已创建的实例。"""
    apps: list[CoreApp] = []

    async def make(*, turns=(), heart_intensity: str = "off") -> CoreApp:
        app = CoreApp(monkeypatch, recorder, turns=turns, heart_intensity=heart_intensity)
        apps.append(app)
        return app

    yield make

    for app in apps:
        await app.stop()
