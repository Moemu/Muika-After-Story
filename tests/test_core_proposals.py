"""Core 多文件提案的常见路径与数据完整性测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from muika.config import mas_config
from muika.core.actions.tools import _filesystem
from muika.core.self_mod.proposals import CoreProposalError, CoreProposalManager


@pytest.fixture
def core_workspace(tmp_path: Path, monkeypatch):
    """创建独立的最小 Core 工作区。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mas_config, "data_dir", Path("data"))
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_core_proposals", True)
    (tmp_path / "muika" / "core").mkdir(parents=True)
    (tmp_path / "muika_bot").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "muika" / "core" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "core_main.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp_path, CoreProposalManager(tmp_path)


def test_create_multifile_proposal_keeps_workspace_unchanged(core_workspace):
    root, manager = core_workspace
    patch_id = manager.create(
        [
            {
                "action": "modify",
                "path": "muika/core/sample.py",
                "replacements": [{"old_text": "VALUE = 1", "new_text": "VALUE = 2"}],
            },
            {"action": "create", "path": "muika/core/new_file.py", "content": "READY = True\n"},
        ],
        "Improve the sample.",
    )

    assert (root / "muika/core/sample.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (root / "muika/core/new_file.py").exists()
    proposal_dir = root / "data/core_proposals" / patch_id
    proposal = json.loads((proposal_dir / "proposal.json").read_text(encoding="utf-8"))
    assert proposal["status"] == "pending"
    assert proposal["audit_errors"] == []
    assert (proposal_dir / "before/muika/core/sample.py").is_file()
    assert (proposal_dir / "after/muika/core/new_file.py").is_file()
    assert "-VALUE = 1" in (proposal_dir / "proposal.diff").read_text(encoding="utf-8")


def test_modify_requires_each_old_text_to_match_once(core_workspace):
    _, manager = core_workspace
    with pytest.raises(CoreProposalError, match="matched 0 times"):
        manager.create(
            [
                {
                    "action": "modify",
                    "path": "muika/core/sample.py",
                    "replacements": [{"old_text": "missing", "new_text": "x"}],
                }
            ],
            "Invalid edit.",
        )


@pytest.mark.parametrize(
    "path",
    [
        "muika/core/self_mod/proposals.py",
        "muika/core/actions/tools/_core_proposal.py",
        "muika/builtin_plugins/patch.py",
        "muika/migrations/new.py",
        "tests/test_core_proposals.py",
        "configs/models.py",
    ],
)
def test_proposal_control_and_outside_paths_are_rejected(core_workspace, path):
    _, manager = core_workspace
    with pytest.raises(CoreProposalError, match="Access denied"):
        manager.resolve_core_path(path, for_write=True)


def test_syntax_error_blocks_proposal(core_workspace):
    _, manager = core_workspace
    with pytest.raises(CoreProposalError, match="syntax check failed"):
        manager.create([{"action": "create", "path": "muika/core/bad.py", "content": "if True\n"}], "Bad.")


def test_show_reports_derived_stale_and_test_warning(core_workspace):
    root, manager = core_workspace
    patch_id = manager.create(
        [{"action": "create", "path": "tests/test_new.py", "content": "def test_ok():\n    assert True\n"}],
        "Add a test.",
    )
    current = manager.show(patch_id)
    assert "过期：no" in current
    assert "修改了测试文件" in current
    (root / "muika/core/sample.py").write_text("VALUE = 3\n", encoding="utf-8")
    assert "过期：yes" in manager.show(patch_id)


@pytest.mark.asyncio
async def test_generic_file_tools_cannot_write_protected_core(core_workspace, monkeypatch):
    root, _ = core_workspace
    monkeypatch.setattr(mas_config, "fs_allowed_paths", [str(root)])
    monkeypatch.setattr(mas_config, "enable_file_write", True)
    target = root / "muika/core/sample.py"

    write_result = await _filesystem.write_file(str(target), "VALUE = 9\n")
    edit_result = await _filesystem.edit_file(str(target), "replace", old_string="VALUE = 1", new_string="VALUE = 9")
    delete_result = await _filesystem.delete_file(str(target))

    assert "protected core code" in write_result
    assert "protected core code" in edit_result
    assert "protected core code" in delete_result
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_dual_switch_is_required(core_workspace, monkeypatch):
    _, manager = core_workspace
    monkeypatch.setattr(mas_config, "enable_core_proposals", False)
    with pytest.raises(CoreProposalError, match="disabled"):
        manager.create([{"action": "create", "path": "muika/core/new.py", "content": "X = 1\n"}], "No.")
