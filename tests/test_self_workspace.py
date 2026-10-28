"""版本工作区门禁、路径路由与指针原语测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muika.config import mas_config
from muika.core.self_mod import workspace as workspace_module
from muika.core.self_mod.workspace import SelfWorkspaceManager


def _make_runtime(tmp_path: Path) -> Path:
    """构造最小假源码树（source 工作区形态）。"""
    root = tmp_path / "runtime"
    (root / "muika").mkdir(parents=True)
    (root / "muika" / "__init__.py").write_text('"""fake runtime"""\n', encoding="utf-8")
    (root / "muika" / "core.py").write_text("CORE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'fake'\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_smoke.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    return root


def _probe(status: str = "completed", failures: list[str] | None = None, test_count: int = 5) -> dict:
    return {
        "status": status,
        "reason": "stub",
        "timed_out": False,
        "output": "",
        "failures": failures or [],
        "errors": [],
        "test_count": test_count,
    }


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    return _make_runtime(tmp_path)


@pytest.fixture
def manager(runtime: Path, monkeypatch) -> SelfWorkspaceManager:
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    return SelfWorkspaceManager(runtime_root=runtime)


@pytest.fixture
def passing_probe(monkeypatch) -> list[Path]:
    """把探针替换为恒通过 stub，并记录调用。"""
    calls: list[Path] = []

    def _fake_run_probe(tree):
        calls.append(tree)
        return _probe()

    monkeypatch.setattr(workspace_module, "_run_probe", _fake_run_probe)
    monkeypatch.setattr(workspace_module, "_baseline_report", lambda fingerprint: _probe())
    return calls


async def test_open_forks_tree_and_records_hashes(manager: SelfWorkspaceManager):
    ws_id = manager.open("learn my own core")

    manifest = manager.load_manifest(ws_id)
    tree = manager.tree_root(ws_id)
    assert manifest["status"] == "open"
    assert manifest["fork_hashes"]["muika/core.py"]
    assert (tree / "muika" / "core.py").read_text(encoding="utf-8") == "CORE = 1\n"


def test_open_rejects_non_source_runtime(tmp_path: Path, monkeypatch):
    bare = tmp_path / "bare"
    (bare / "muika").mkdir(parents=True)
    (bare / "muika" / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    manager = SelfWorkspaceManager(runtime_root=bare)

    with pytest.raises(workspace_module.SelfWorkspaceError, match="source"):
        manager.open("try anyway")


async def test_submit_whitelist_rejects_control_paths(manager: SelfWorkspaceManager):
    ws_id = manager.open("smuggle")
    tree = manager.tree_root(ws_id)
    forged = tree / "data" / "reviews" / "deadbeef.json"
    forged.parent.mkdir(parents=True, exist_ok=True)
    forged.write_text("{}", encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="data/reviews/deadbeef.json"):
        await manager.submit(ws_id, "submit forged approval")
    forged.unlink()

    for rel in (".env", "plugins/evil.py"):
        target = tree / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="Rejected path in this change"):
        await manager.submit(ws_id, "submit smuggled paths")


async def test_submit_reports_unsupported_dependencies(manager: SelfWorkspaceManager):
    ws_id = manager.open("dependency change")
    tree = manager.tree_root(ws_id)
    (tree / "pyproject.toml").write_text("[project]\nname = 'fake2'\n", encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="not supported"):
        await manager.submit(ws_id, "change dependency")


async def test_submit_escalate_suspends_for_player(manager: SelfWorkspaceManager, monkeypatch):
    from muika.core.code_review import get_code_reviewer

    ws_id = manager.open("schema change")
    migration = manager.tree_root(ws_id) / "muika" / "migrations" / "versions" / "0001_add.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("def upgrade():\n    pass\n", encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="review"):
        await manager.submit(ws_id, "add migration")

    records = [r for r in get_code_reviewer().records() if r.status == "pending"]
    assert records
    record = records[-1]
    assert record.require_player is True
    payload = record.payload
    assert payload["touched_migrations"] is True
    assert "记忆" in str(payload.get("warning", ""))


async def test_submit_auto_review_passes_and_writes_pointer_last(
    manager: SelfWorkspaceManager, passing_probe, approved_review, monkeypatch, tmp_path
):
    from muika.ipc import supervisor

    monkeypatch.setenv("DATA_DIR", str(mas_config.data_dir.resolve()))
    ws_id = manager.open("tune my core")
    (manager.tree_root(ws_id) / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")

    events: list[str] = []
    original_save = manager._save_manifest

    def _spy_save(manifest):
        original_save(manifest)
        events.append("manifest")

    monkeypatch.setattr(manager, "_save_manifest", _spy_save)
    original_pointer = supervisor.write_boot_target

    def _spy_pointer(*args, **kwargs):
        original_pointer(*args, **kwargs)
        events.append("pointer")

    monkeypatch.setattr(supervisor, "write_boot_target", _spy_pointer)

    report = await manager.submit(ws_id, "tune core")

    assert events == ["manifest", "pointer"]
    assert "restart" in report.lower()
    manifest = manager.load_manifest(ws_id)
    assert manifest["status"] == "submitted"
    assert manifest["touched"][0]["path"] == "muika/core.py"
    assert manifest["touched"][0]["escalate"] is False
    pointer_path = supervisor.read_boot_target()
    assert pointer_path is not None
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer["workspace_id"] == ws_id
    assert pointer["review_id"]
    assert Path(manifest["backup_path"]).is_file() or manifest["backup_path"] == ""


async def test_submit_conflict_refuses_drifted_touched_file(manager: SelfWorkspaceManager, monkeypatch):
    ws_id = manager.open("edit core")
    (manager.tree_root(ws_id) / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")
    (manager._runtime_root / "muika" / "core.py").write_text("CORE = 99\n", encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="muika/core.py"):
        await manager.submit(ws_id, "edit core")


async def test_submit_refreshes_untouched_drift(
    manager: SelfWorkspaceManager, passing_probe, approved_review, monkeypatch
):
    monkeypatch.setenv("DATA_DIR", str(mas_config.data_dir.resolve()))
    ws_id = manager.open("edit core")
    tree = manager.tree_root(ws_id)
    (tree / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")
    (manager._runtime_root / "tests" / "test_extra.py").write_text(
        "def test_new():\n    assert True\n", encoding="utf-8"
    )

    await manager.submit(ws_id, "edit core")

    assert (tree / "tests" / "test_extra.py").read_text(encoding="utf-8").startswith("def test_new")
    manifest = manager.load_manifest(ws_id)
    assert [change["path"] for change in manifest["touched"]] == ["muika/core.py"]


async def test_submit_serial_gate_blocks_second_workspace(manager: SelfWorkspaceManager, monkeypatch, runtime: Path):
    from muika.ipc import supervisor

    monkeypatch.setenv("DATA_DIR", str(mas_config.data_dir.resolve()))
    first = manager.open("first")
    (manager.tree_root(first) / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")
    supervisor.write_boot_target(first, "0" * 64, "0" * 64, "")

    second = manager.open("second")
    with pytest.raises(workspace_module.SelfWorkspaceError, match="rollback|discard"):
        await manager.submit(second, "second change")

    manager.discard(second, "not needed")
    assert supervisor.read_boot_target() is not None
    manager.discard(first, "clear pending")
    assert supervisor.read_boot_target() is None


async def test_before_snapshot_keeps_runtime_content(
    manager: SelfWorkspaceManager, passing_probe, approved_review, monkeypatch
):
    monkeypatch.setenv("DATA_DIR", str(mas_config.data_dir.resolve()))
    ws_id = manager.open("edit core")
    (manager.tree_root(ws_id) / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")

    await manager.submit(ws_id, "edit core")

    before = manager.workspace_root(ws_id) / "before" / "muika" / "core.py"
    assert before.read_text(encoding="utf-8") == "CORE = 1\n"


async def test_probe_blocks_new_failures_and_empty_suites(manager: SelfWorkspaceManager, monkeypatch, approved_review):
    ws_id = manager.open("edit core")
    (manager.tree_root(ws_id) / "muika" / "core.py").write_text("CORE = 2\n", encoding="utf-8")

    monkeypatch.setattr(workspace_module, "_baseline_report", lambda fp: _probe())
    monkeypatch.setattr(workspace_module, "_run_probe", lambda tree: _probe(failures=["tests/test_x.py::test_y"]))
    with pytest.raises(workspace_module.SelfWorkspaceError, match="test_x"):
        await manager.submit(ws_id, "edit core")

    monkeypatch.setattr(workspace_module, "_run_probe", lambda tree: _probe(test_count=0))
    with pytest.raises(workspace_module.SelfWorkspaceError, match="test"):
        await manager.submit(ws_id, "edit core")


# ---------- code_review.require_player ----------


async def test_authorize_require_player_never_auto_approves(monkeypatch):
    from muika.core.code_review import CodeReviewer, ReviewError, get_code_reviewer

    async def fail_assess(record):
        raise AssertionError("assess must not run for player-gated requests")

    monkeypatch.setattr(CodeReviewer, "assess", fail_assess)
    monkeypatch.setattr(mas_config, "code_review_mode", "auto")

    with pytest.raises(ReviewError, match="review approve"):
        await get_code_reviewer().authorize("plugin", {"x": 1}, require_player=True)

    records = [r for r in get_code_reviewer().records() if r.status == "pending"]
    assert records
    assert records[-1].require_player is True


async def test_player_gated_request_expires(monkeypatch):
    from datetime import datetime, timedelta

    from muika.core.code_review import ReviewError, get_code_reviewer

    monkeypatch.setattr(mas_config, "code_review_mode", "auto")
    payload = {"x": 1}
    with pytest.raises(ReviewError):
        await get_code_reviewer().authorize("plugin", payload, require_player=True)
    reviewer = get_code_reviewer()
    record = reviewer.records()[-1]
    stale = record.model_copy(update={"created_at": (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")})
    reviewer.save(stale)

    with pytest.raises(ReviewError, match="expired"):
        await get_code_reviewer().authorize("plugin", payload, require_player=True)


# ---------- 文件工具有序路由 ----------


async def test_write_file_routes_persona_writes_through_journal(monkeypatch, tmp_path):
    from muika.core.actions.tools import _filesystem as fs

    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])

    result = await fs.write_file("templates/note.jinja2", "Hello {{ name }}\n", "overwrite", reason="keep a note")
    assert "rejected" not in result or "File written" in result
    assert (tmp_path / "templates" / "note.jinja2").exists()

    rejected = await fs.write_file("templates/note.jinja2", "x", "overwrite")
    assert "reason" in rejected


async def test_write_file_routes_plugins_to_deployer(monkeypatch):
    from muika.core.actions.tools import _filesystem as fs
    from muika.core.self_mod.plugin_deployer import PluginDeployer

    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])
    calls: list[tuple] = []

    async def fake_deploy_new(self, path, content, reason):
        calls.append((path, content, reason))
        return "staged"

    monkeypatch.setattr(PluginDeployer, "deploy_new", fake_deploy_new)

    await fs.write_file("plugins/my_tool.py", "VALUE = 1\n", "overwrite", reason="new plugin")
    assert calls == [("plugins/my_tool.py", "VALUE = 1\n", "new plugin")]


async def test_workspace_session_grants_direct_writes(manager: SelfWorkspaceManager, monkeypatch):
    from muika.core.actions.tools import _filesystem as fs

    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])
    ws_id = manager.open("free edit")

    result = await fs.write_file("muika/core.py", "CORE = 42\n", "overwrite")
    assert "written" in result.lower()
    assert (manager.tree_root(ws_id) / "muika" / "core.py").read_text(encoding="utf-8") == "CORE = 42\n"


async def test_default_tree_write_stays_protected(monkeypatch, runtime: Path):
    from muika.core.actions.tools import _filesystem as fs
    from muika.core.self_mod.policy import _runtime_source_root

    repo = _runtime_source_root()
    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(repo)])

    result = await fs.write_file(str(repo / "muika" / "core" / "loop.py"), "boom\n", "overwrite")
    assert "protected" in result


async def test_runtime_read_scope_excludes_sensitive_files(monkeypatch):
    from muika.core.actions.tools import _filesystem as fs
    from muika.core.self_mod.policy import _runtime_source_root

    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])
    repo = _runtime_source_root()

    code = await fs.read_file(str(repo / "muika" / "core" / "restart.py"), max_chars=200)
    assert "restart" in code.lower() or "File:" in code

    denied = await fs.read_file(str(repo / ".env"), max_chars=50)
    assert "denied" in denied or "protected" in denied


async def test_runtime_read_scope_excludes_models_config(monkeypatch):
    from muika.core.actions.tools import _filesystem as fs
    from muika.core.self_mod.policy import _runtime_source_root

    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])
    repo = _runtime_source_root()

    denied = await fs.read_file(str(repo / "configs" / "models.yml"), max_chars=50)
    assert "denied" in denied or "protected" in denied


# ---------- 复审修复回归 ----------


async def test_open_requires_self_modify(manager: SelfWorkspaceManager, monkeypatch):
    monkeypatch.setattr(mas_config, "action_permission", "write")
    with pytest.raises(workspace_module.SelfWorkspaceError, match="disabled"):
        manager.open("should not fork")


async def test_submit_requires_self_modify(manager: SelfWorkspaceManager, monkeypatch):
    ws_id = manager.open("legit under self_modify")
    monkeypatch.setattr(mas_config, "action_permission", "write")
    with pytest.raises(workspace_module.SelfWorkspaceError, match="disabled"):
        await manager.submit(ws_id, "downgraded before submit")


async def test_submit_rejects_oversized_diff(manager: SelfWorkspaceManager, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(mas_config.data_dir.resolve()))
    ws_id = manager.open("one huge line")
    huge = "CORE = '" + "x" * 60_000 + "'\n"
    (manager.tree_root(ws_id) / "muika" / "core.py").write_text(huge, encoding="utf-8")

    with pytest.raises(workspace_module.SelfWorkspaceError, match="exceeds"):
        await manager.submit(ws_id, "unreviewable size")


async def test_baseline_cache_never_poisons_submit(monkeypatch, tmp_path):
    monkeypatch.setattr(mas_config, "data_dir", tmp_path / "data")
    calls = []

    def fake_probe(tree):
        calls.append(tree)
        return _probe(status="failed", test_count=0) | {"timed_out": True, "reason": "timed out"}

    monkeypatch.setattr(workspace_module, "_run_probe", fake_probe)
    monkeypatch.setattr(workspace_module, "_deployment_probe_environment", lambda: None)
    fingerprint = "test-fingerprint"

    first = workspace_module._baseline_report(fingerprint)
    assert first["timed_out"] is True
    cache = mas_config.workspaces_dir / "_baseline" / f"{fingerprint}.json"
    assert not cache.exists(), "timed-out baseline must not be cached"

    # 环境缺失导致的 unavailable 会缓存；环境恢复后必须重测而不是永久拒绝
    monkeypatch.setattr(workspace_module, "_deployment_probe_environment", lambda: "pytest missing")
    unavailable = workspace_module._baseline_report(fingerprint)
    assert unavailable["status"] == "unavailable"
    assert cache.exists()

    monkeypatch.setattr(workspace_module, "_deployment_probe_environment", lambda: None)
    monkeypatch.setattr(workspace_module, "_run_probe", lambda tree: _probe())
    recovered = workspace_module._baseline_report(fingerprint)
    assert recovered["status"] == "completed"


async def test_list_directory_covers_runtime_tree(monkeypatch):
    from muika.core.actions.tools import _filesystem as fs
    from muika.core.self_mod.policy import _runtime_source_root

    monkeypatch.setattr(mas_config, "action_permission", "self_modify")
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [])

    result = await fs.list_directory(str(_runtime_source_root() / "muika"))
    assert "DIR" in result or "FILE" in result


def test_tree_hashes_skips_dependency_and_cache_paths(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "lib.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "muika.db").write_bytes(b"db")
    (tmp_path / "muika").mkdir()
    (tmp_path / "muika" / "core.py").write_text("CORE = 1\n", encoding="utf-8")

    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "core.cpython.pyc").write_bytes(b"pyc")
    assert set(workspace_module._tree_hashes(tmp_path)) == {"muika/core.py"}
    assert set(workspace_module._tree_hashes(tmp_path, skip=workspace_module._RUN_ARTIFACT_PATTERNS)) == {
        ".venv/lib.py",
        "data/muika.db",
        "muika/core.py",
    }
