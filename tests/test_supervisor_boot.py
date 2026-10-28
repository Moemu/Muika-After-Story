"""supervisor 指针启动、提升、回退、回滚与自动回滚测试。"""

from __future__ import annotations

import hashlib
import json
import textwrap
from pathlib import Path
from typing import cast

import pytest

from muika.ipc import supervisor
from muika.ipc.supervisor import (
    RestartRecord,
    _build_child_env,
    _execute_rollback,
    _ready_matches,
    _try_auto_rollback,
    classify_change,
    promote_workspace,
    read_boot_target,
    read_rollback_ready,
    restore_workspace,
    verify_review,
    write_boot_target,
    write_rollback_ready,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _workspace(data_dir: Path, ws_id: str, *, touched: list[dict], review_status: str = "approved") -> Path:
    """构造一个带清单、审批记录与指针的工作区。"""
    root = data_dir / "workspaces" / ws_id
    tree = root / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    for change in touched:
        target = tree / change["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if change.get("sha_after") is not None:
            target.write_bytes(change["_after_text"].encode("utf-8"))
        if change.get("sha_before") is not None:
            before = root / "before" / change["path"]
            before.parent.mkdir(parents=True, exist_ok=True)
            before.write_bytes(change["_before_text"].encode("utf-8"))
    review_id = "a" * 64
    _write_json(
        data_dir / "reviews" / f"{review_id}.json",
        {
            "id": review_id,
            "kind": "workspace",
            "payload": {
                "workspace_id": ws_id,
                "changes": [
                    {
                        "path": c["path"],
                        "action": c["action"],
                        "escalate": False,
                        "sha_before": c.get("sha_before"),
                        "sha_after": c.get("sha_after"),
                    }
                    for c in touched
                ],
            },
            "status": review_status,
        },
    )
    _write_json(
        root / "manifest.json",
        {
            "schema_version": 1,
            "ws_id": ws_id,
            "reason": "test",
            "status": "submitted",
            "created_at": "2026-10-25T00:00:00",
            "fork_hashes": {},
            "touched": [{k: v for k, v in c.items() if not k.startswith("_")} for c in touched],
            "touched_migrations": any(c["path"].startswith("muika/migrations/") for c in touched),
            "backup_path": "",
            "review_id": review_id,
            "submitted_at": "2026-10-25T00:00:01",
        },
    )
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest_digest = hashlib.sha256(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    write_boot_target(ws_id, manifest_digest, review_id, "")
    return root


def _stub_package(root: Path, mode: str) -> None:
    """写入最小 muika 桩包；bootstrap 按 mode 行事。"""
    (root / "muika" / "ipc").mkdir(parents=True, exist_ok=True)
    (root / "muika" / "__init__.py").write_text("", encoding="utf-8")
    (root / "muika" / "ipc" / "__init__.py").write_text("", encoding="utf-8")
    if mode == "bad":
        body = "import sys\nsys.exit(1)\n"
    else:
        body = textwrap.dedent("""
            import json, os, time
            from pathlib import Path
            directory = os.environ["MUIKA_LIFECYCLE_DIR"]
            payload = {"pid": os.getpid(), "nonce": os.environ["MUIKA_BOOT_NONCE"], "running_root": str(Path.cwd())}
            Path(directory, "ready.json").write_text(json.dumps(payload), encoding="utf-8")
            time.sleep(1)
            """)
    (root / "muika" / "ipc" / "bootstrap.py").write_text(body, encoding="utf-8")


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("DATA_DIR", str(root))
    return root


# ---------- 纯函数 ----------


def test_classify_change_whitelist_and_escalate():
    assert classify_change("muika/core/brain.py") == "promotable"
    assert classify_change("muika/core/loop.py") == "escalated"
    assert classify_change("configs/models.yml") == "escalated"
    assert classify_change("muika/migrations/versions/x.py") == "escalated"
    assert classify_change("tests/test_a.py") == "escalated"
    assert classify_change("ipc/supervisor.py") == "rejected"
    assert classify_change("data/reviews/x.json") == "rejected"
    assert classify_change(".env") == "rejected"
    assert classify_change("plugins/x.py") == "rejected"
    assert classify_change("pyproject.toml") == "unsupported"


def test_build_child_env_merges_env_file_and_forces_data_dir(tmp_path: Path, monkeypatch, data_dir: Path):
    default_root = tmp_path / "repo"
    default_root.mkdir()
    (default_root / ".env").write_text("FOO=from_env_file\nBAZ=from_env_file\n", encoding="utf-8")
    monkeypatch.delenv("FOO", raising=False)
    monkeypatch.setenv("BAZ", "from_os_environ")

    env = _build_child_env(default_root)

    assert env["FOO"] == "from_env_file"
    assert env["BAZ"] == "from_os_environ"
    assert Path(env["DATA_DIR"]) == data_dir
    assert env["MUIKA_BOOT_NONCE"]


def test_ready_matches_validates_nonce_and_root(tmp_path: Path):
    good = tmp_path / "good.json"
    _write_json(good, {"nonce": "n1", "running_root": str(tmp_path)})
    assert _ready_matches(good, tmp_path, "n1")
    assert not _ready_matches(good, tmp_path, "other")
    assert not _ready_matches(good, tmp_path / "elsewhere", "n1")
    wrong = tmp_path / "wrong.json"
    wrong.write_text("not json", encoding="utf-8")
    assert not _ready_matches(wrong, tmp_path, "n1")
    assert _ready_matches(good, None, "n1")


def test_verify_review_requires_matching_approved_record(data_dir: Path, tmp_path: Path):
    ws = _workspace(data_dir, "20261025_000000_00000001", touched=[])
    manifest = json.loads((ws / "manifest.json").read_text(encoding="utf-8"))

    verify_review(manifest["review_id"], manifest["ws_id"], manifest["touched"])

    missing = dict(manifest, review_id="b" * 64)
    with pytest.raises(ValueError, match="[Rr]eview"):
        verify_review(missing["review_id"], missing["ws_id"], missing["touched"])
    denied_ws = _workspace(data_dir, "20261025_000000_00000002", touched=[], review_status="denied")
    denied = json.loads((denied_ws / "manifest.json").read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        verify_review(denied["review_id"], denied["ws_id"], denied["touched"])


# ---------- 提升与还原（tmp default 树） ----------


def _tri_state_fixture(tmp_path: Path):
    default = tmp_path / "default"
    (default / "muika").mkdir(parents=True)
    (default / "muika" / "a.py").write_bytes(b"A0\n")
    (default / "muika" / "b.py").write_bytes(b"B1\n")  # 已写过的 after
    (default / "muika" / "c.py").write_bytes(b"C0\n")
    touched = [
        {"path": "muika/a.py", "action": "modify", "sha_before": _sha("A0\n"), "sha_after": _sha("A1\n")},
        {"path": "muika/b.py", "action": "modify", "sha_before": _sha("B0\n"), "sha_after": _sha("B1\n")},
        {"path": "muika/c.py", "action": "modify", "sha_before": _sha("C0\n"), "sha_after": _sha("C1\n")},
    ]
    for change, after, before in zip(touched, ("A1\n", "B1\n", "C1\n"), ("A0\n", "B0\n", "C0\n")):
        change["_after_text"] = after
        change["_before_text"] = before
    return default, touched


def test_promote_workspace_tri_state_conflict_and_retry(data_dir: Path, tmp_path: Path):
    default, touched = _tri_state_fixture(tmp_path)
    _workspace(data_dir, "20261025_000000_00000003", touched=touched)

    (default / "muika" / "c.py").write_bytes(b"CX\n")  # 外部漂移制造冲突
    with pytest.raises(ValueError, match="muika/c.py"):
        promote_workspace("20261025_000000_00000003", default)
    record = read_rollback_ready()
    assert record and record["status"] == "pending"
    # 统一失败路径已回填已写文件
    assert (default / "muika" / "a.py").read_text(encoding="utf-8") == "A0\n"
    assert read_boot_target() is not None

    (default / "muika" / "c.py").write_bytes(b"C0\n")
    promote_workspace("20261025_000000_00000003", default)

    assert (default / "muika" / "a.py").read_text(encoding="utf-8") == "A1\n"
    assert (default / "muika" / "b.py").read_text(encoding="utf-8") == "B1\n"  # ==after 跳过未重写
    ready_record = read_rollback_ready()
    assert ready_record is not None and ready_record["status"] == "ready"


def test_promote_workspace_requires_approved_review(data_dir: Path, tmp_path: Path):
    default, touched = _tri_state_fixture(tmp_path)
    _workspace(data_dir, "20261025_000000_00000004", touched=touched, review_status="pending")

    with pytest.raises(ValueError):
        promote_workspace("20261025_000000_00000004", default)
    assert (default / "muika" / "a.py").read_text(encoding="utf-8") == "A0\n"


def test_restore_workspace_restores_archives_and_database(data_dir: Path, tmp_path: Path):
    default = tmp_path / "default"
    (default / "muika").mkdir(parents=True)
    (default / "muika" / "x.py").write_bytes(b"X1\n")
    (default / "muika" / "new.py").write_bytes(b"N1\n")
    backup = data_dir / "backups" / "ws.db"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text("sqlite-backup-bytes", encoding="utf-8")
    db = data_dir / "muika.db"
    db.write_text("live", encoding="utf-8")
    (data_dir / "muika.db-wal").write_text("wal", encoding="utf-8")
    before_dir = tmp_path / "before"
    (before_dir / "muika").mkdir(parents=True)
    (before_dir / "muika" / "x.py").write_bytes(b"X0\n")
    record = {
        "workspace_id": "20261025_000000_00000005",
        "touched": [
            {"path": "muika/x.py", "action": "modify", "sha_before": _sha("X0\n"), "sha_after": _sha("X1\n")},
            {"path": "muika/new.py", "action": "create", "sha_before": None, "sha_after": _sha("N1\n")},
        ],
        "touched_migrations": True,
        "backup_path": str(backup),
        "before_dir": str(before_dir),
        "status": "ready",
    }
    archive = tmp_path / "archive"

    restored = restore_workspace(record, default, archive_after_dir=archive)

    assert set(restored) == {"muika/x.py", "muika/new.py"}
    assert (default / "muika" / "x.py").read_text(encoding="utf-8") == "X0\n"
    assert not (default / "muika" / "new.py").exists()
    assert (archive / "muika" / "x.py").read_text(encoding="utf-8") == "X1\n"
    assert db.read_text(encoding="utf-8") == "sqlite-backup-bytes"
    assert not (data_dir / "muika.db-wal").exists()


def test_execute_rollback_gates_on_memory_loss(data_dir: Path, tmp_path: Path):
    default = tmp_path / "default"
    (default / "muika").mkdir(parents=True)
    (default / "muika" / "x.py").write_bytes(b"X1\n")
    before_dir = tmp_path / "before"
    (before_dir / "muika").mkdir(parents=True)
    (before_dir / "muika" / "x.py").write_bytes(b"X0\n")
    record = {
        "workspace_id": "20261025_000000_00000006",
        "touched": [{"path": "muika/x.py", "action": "modify", "sha_before": _sha("X0\n"), "sha_after": _sha("X1\n")}],
        "touched_migrations": True,
        "backup_path": "",
        "before_dir": str(before_dir),
        "status": "ready",
    }
    write_rollback_ready(record)
    restart = cast(
        "RestartRecord",
        {
            "id": "r1",
            "workspace_id": record["workspace_id"],
            "reason": "rollback",
            "trigger": "rollback",
            "record_path": str(data_dir / "restart.json"),
            "status": "restarting",
        },
    )

    _execute_rollback(restart, default)

    saved = json.loads((data_dir / "restart.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert "confirm" in saved["error"].lower() or "--confirm-memory-loss" in saved["error"]
    assert (default / "muika" / "x.py").read_text(encoding="utf-8") == "X1\n"
    assert read_rollback_ready() is not None

    restart["confirm_memory_loss"] = True
    _execute_rollback(restart, default)

    saved = json.loads((data_dir / "restart.json").read_text(encoding="utf-8"))
    assert saved["status"] == "rolled_back"
    assert (default / "muika" / "x.py").read_text(encoding="utf-8") == "X0\n"
    assert read_rollback_ready() is None


def test_try_auto_rollback_only_for_matching_promoted_state(data_dir: Path, tmp_path: Path):
    default = tmp_path / "default"
    (default / "muika").mkdir(parents=True)
    (default / "muika" / "x.py").write_bytes(b"X1\n")
    before_dir = tmp_path / "before"
    (before_dir / "muika").mkdir(parents=True)
    (before_dir / "muika" / "x.py").write_bytes(b"X0\n")
    record = {
        "workspace_id": "20261025_000000_00000007",
        "touched": [{"path": "muika/x.py", "action": "modify", "sha_before": _sha("X0\n"), "sha_after": _sha("X1\n")}],
        "touched_migrations": False,
        "backup_path": "",
        "before_dir": str(before_dir),
        "status": "ready",
    }
    write_rollback_ready(record)
    (default / "muika" / "x.py").write_bytes(b"DRIFTED\n")
    assert _try_auto_rollback(default) is False

    (default / "muika" / "x.py").write_bytes(b"X1\n")
    assert _try_auto_rollback(default) is True
    assert (default / "muika" / "x.py").read_text(encoding="utf-8") == "X0\n"
    consumed = read_rollback_ready()
    assert consumed is not None and consumed["status"] == "consumed"


# ---------- supervise() 真进程回路 ----------


def _run_supervise(monkeypatch, good_default: Path, timeout: int = 6):
    monkeypatch.setenv("PYTHONPATH", str(good_default))
    monkeypatch.setattr(supervisor, "STARTUP_TIMEOUT", timeout)
    return supervisor.supervise([])


def test_pointer_boot_promotes_then_supervise_exits(data_dir: Path, tmp_path: Path, monkeypatch):
    good = tmp_path / "good_default"
    _stub_package(good, "good")
    ws = _workspace(data_dir, "20261025_000000_00000008", touched=[])
    _stub_package(ws / "tree", "good")

    code = _run_supervise(monkeypatch, good)

    assert code == 0
    assert read_boot_target() is None
    record = read_rollback_ready()
    assert record and record["status"] == "ready"
    restart = json.loads((data_dir / "restart.json").read_text(encoding="utf-8"))
    assert restart["status"] == "started"


def test_broken_pointer_falls_back_to_default(data_dir: Path, tmp_path: Path, monkeypatch):
    good = tmp_path / "good_default"
    _stub_package(good, "good")
    ws = _workspace(data_dir, "20261025_000000_00000009", touched=[])
    _stub_package(ws / "tree", "bad")

    code = _run_supervise(monkeypatch, good)

    assert code == 0
    assert read_boot_target() is None


def test_promoted_default_failure_auto_rolls_back_once(data_dir: Path, tmp_path: Path, monkeypatch):
    bad = tmp_path / "bad_default"
    _stub_package(bad, "bad")
    before_dir = tmp_path / "before"
    write_rollback_ready(
        {
            "workspace_id": "20261025_000000_00000010",
            "touched": [],
            "touched_migrations": False,
            "backup_path": "",
            "before_dir": str(before_dir),
            "status": "ready",
        }
    )

    monkeypatch.setenv("PYTHONPATH", str(bad))
    monkeypatch.setattr(supervisor, "STARTUP_TIMEOUT", 3)
    code = supervisor.supervise([])

    assert code != 0
    record = read_rollback_ready()
    assert record and record["status"] == "consumed"
    restart = json.loads((data_dir / "restart.json").read_text(encoding="utf-8"))
    assert restart["status"] == "auto_rolled_back"


def test_classify_change_rejects_dotdot_paths():
    assert classify_change("muika/../data/x") == "rejected"


def test_rollback_restart_trigger_matches_patch_command():
    from muika.builtin_plugins import patch as patch_plugin

    assert supervisor.ROLLBACK_TRIGGER == "rollback"
    source = Path(patch_plugin.__file__).read_text(encoding="utf-8")
    assert "supervisor.ROLLBACK_TRIGGER" in source


def test_execute_rollback_refuses_drifted_default(data_dir: Path, tmp_path: Path):
    default = tmp_path / "default"
    (default / "muika").mkdir(parents=True)
    (default / "muika" / "x.py").write_bytes(b"DRIFTED\n")
    before_dir = tmp_path / "before"
    (before_dir / "muika").mkdir(parents=True)
    (before_dir / "muika" / "x.py").write_bytes(b"X0\n")
    record = {
        "workspace_id": "20261025_000000_00000008",
        "touched": [{"path": "muika/x.py", "action": "modify", "sha_before": _sha("X0\n"), "sha_after": _sha("X1\n")}],
        "touched_migrations": False,
        "backup_path": "",
        "before_dir": str(before_dir),
        "status": "ready",
    }
    write_rollback_ready(record)
    restart = cast(
        "RestartRecord",
        {
            "id": "r2",
            "workspace_id": record["workspace_id"],
            "reason": "rollback",
            "trigger": "rollback",
            "record_path": str(data_dir / "restart.json"),
            "status": "restarting",
        },
    )

    _execute_rollback(restart, default)

    saved = json.loads((data_dir / "restart.json").read_text(encoding="utf-8"))
    assert saved["status"] == "failed"
    assert "no longer matches" in saved["error"] or "drift" in saved["error"].lower()
    assert (default / "muika" / "x.py").read_text(encoding="utf-8") == "DRIFTED\n"
    assert read_rollback_ready() is not None
