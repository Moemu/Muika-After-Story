from pathlib import Path

import pytest

from muika.config import mas_config
from muika.core.self_mod import SelfModError
from muika.core.self_mod import manager as manager_module
from muika.core.self_mod.manager import SelfModManager
from muika.core.self_mod.policy import resolve_self_path
from muika.database.crud import SelfModificationCRUD


def _use_test_session(monkeypatch, db_session, session_ctx_factory) -> None:
    monkeypatch.setattr(manager_module, "get_session", lambda: session_ctx_factory(db_session))


def test_builtin_template_is_read_only(monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    path = Path(manager_module.__file__).resolve().parents[2] / "builtin_templates/Muika.md.jinja2"

    assert resolve_self_path(str(path)) == path
    with pytest.raises(SelfModError, match="protected core code"):
        resolve_self_path(str(path), require_write=True)


def test_project_skill_override_is_writable(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    path = tmp_path / "configs/skills/muika-self/SKILL.md"

    assert resolve_self_path(str(path), require_write=True) == path


@pytest.mark.asyncio
async def test_revert_validation_failure_keeps_revision_applied(
    tmp_path: Path,
    monkeypatch,
    db_session,
    session_ctx_factory,
):
    _use_test_session(monkeypatch, db_session, session_ctx_factory)
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_plugin_self_modification", True)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "sample.py"
    target.write_text("import os\n", encoding="utf-8")
    manager = SelfModManager()
    await manager.apply(str(target), "VALUE = 2\n", "replace sample")
    monkeypatch.setattr(mas_config, "plugin_import_blacklist", [*mas_config.plugin_import_blacklist, "os"])

    with pytest.raises(SelfModError, match="blacklisted import"):
        await manager.revert(str(target))

    record = await SelfModificationCRUD.latest_write_for_path(db_session, "plugins/sample.py")
    assert record is not None
    assert record.status == "applied"
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_revert_write_failure_keeps_revision_applied(
    tmp_path: Path,
    monkeypatch,
    db_session,
    session_ctx_factory,
):
    _use_test_session(monkeypatch, db_session, session_ctx_factory)
    monkeypatch.setattr(mas_config, "enable_self_modification", True)
    monkeypatch.setattr(mas_config, "enable_plugin_self_modification", True)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    target = plugins / "sample.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    manager = SelfModManager()
    await manager.apply(str(target), "VALUE = 2\n", "replace sample")

    def fail_write(resolved: Path, content: str) -> None:
        raise OSError("locked")

    monkeypatch.setattr(manager, "_atomic_write", fail_write)

    with pytest.raises(OSError, match="locked"):
        await manager.revert(str(target))

    record = await SelfModificationCRUD.latest_write_for_path(db_session, "plugins/sample.py")
    assert record is not None
    assert record.status == "applied"
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
