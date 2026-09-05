"""验证包内协议读取和既有同意记录的兼容性。"""

import json
from dataclasses import asdict

import pytest

from muika.agreement import main
from muika.utils import first_run


def test_agreement_uses_package_resource_from_another_working_directory(tmp_path):
    legacy_file = tmp_path / "configs" / "user_agreement.json"
    legacy_file.parent.mkdir()
    legacy_file.write_text('{"title":"old override","text":"old text","updated":"2099-01-01"}', encoding="utf-8")
    content = first_run.load_agreement_content()
    assert content.title != "old override"
    assert content.text != "old text"
    assert content.updated == "2026-02-01"
    assert legacy_file.exists()


@pytest.mark.parametrize(
    "content",
    [
        None,
        "invalid json",
        "[]",
        '{"title":1,"text":"text","updated":"2026-02-01"}',
        '{"title":"title","text":"text","updated":"invalid"}',
    ],
)
def test_invalid_package_resource_reports_installation_error(tmp_path, monkeypatch, content):
    if content is not None:
        (tmp_path / "user_agreement.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(first_run, "files", lambda package: tmp_path)
    with pytest.raises(RuntimeError, match="Reinstall Muika-After-Story"):
        first_run.load_agreement_content()


@pytest.mark.parametrize("version", ["2026-02-01", "2026-03-01"])
def test_existing_consent_remains_valid_without_rewriting(tmp_path, version):
    path = tmp_path / "user_agreement.json"
    original = json.dumps({"has_agreed": True, "timestamp": "2026-03-01T12:00:00", "version": version})
    path.write_text(original, encoding="utf-8")
    status = first_run.UserAgreement(path).status()
    assert not status.needs_acceptance
    assert path.read_text(encoding="utf-8") == original
    assert status.state.has_agreed


def test_acceptance_creates_data_directory(tmp_path):
    path = tmp_path / "new" / "data" / "user_agreement.json"
    agreement = first_run.UserAgreement(path)
    assert agreement.status().needs_acceptance
    state = agreement.accept("2026-02-01")
    assert json.loads(path.read_text(encoding="utf-8")) == asdict(state)
    assert not agreement.status().needs_acceptance


@pytest.mark.parametrize("version", ["2026-01-01", "", "broken", "2026-02-01T00:00:00+00:00"])
def test_outdated_or_incomparable_versions_require_acceptance(tmp_path, version):
    path = tmp_path / "user_agreement.json"
    path.write_text(json.dumps({"has_agreed": True, "timestamp": "2026-03-01", "version": version}))
    assert first_run.UserAgreement(path).status().needs_acceptance


@pytest.mark.parametrize(
    "content",
    ["broken", "[]", '{"has_agreed":true}', '{"has_agreed":"false","timestamp":"2026-03-01","version":"2026-02-01"}'],
)
def test_corrupt_record_never_partially_accepts(tmp_path, content):
    path = tmp_path / "user_agreement.json"
    path.write_text(content)
    status = first_run.UserAgreement(path).status()
    assert status.needs_acceptance
    assert status.state_error
    assert not status.state.has_agreed


def test_acceptance_rejects_version_not_shown(tmp_path):
    path = tmp_path / "user_agreement.json"
    with pytest.raises(ValueError, match="version changed"):
        first_run.UserAgreement(path).accept("2026-01-01")
    assert not path.exists()


def test_failed_replacement_preserves_old_record(tmp_path, monkeypatch):
    path = tmp_path / "user_agreement.json"
    path.write_text("old record")

    def fail_replace(self, target):
        raise PermissionError("read only")

    monkeypatch.setattr(type(path), "replace", fail_replace)
    with pytest.raises(PermissionError):
        first_run.UserAgreement(path).accept("2026-02-01")
    assert path.read_text() == "old record"
    assert list(tmp_path.iterdir()) == [path]


def test_cli_uses_instance_data_settings(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text('data_dir="from dotenv"\nMASTER_ID=\nIPC_SECRET=\n')
    monkeypatch.delenv("DATA_DIR", raising=False)
    assert main(["accept", "--version", "2026-02-01"]) == 0
    assert (tmp_path / "from dotenv" / "user_agreement.json").exists()
    monkeypatch.setenv("DATA_DIR", "from environment")
    assert main(["status"]) == 0
    status = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert status["needs_acceptance"]
    assert main(["accept", "--version", status["content"]["updated"]]) == 0
    assert (tmp_path / "from environment" / "user_agreement.json").exists()
    assert (tmp_path / ".env").read_text().endswith("IPC_SECRET=\n")


@pytest.mark.parametrize("answer", ["no", "", None])
def test_cli_decline_and_eof_leave_no_record(tmp_path, monkeypatch, answer):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))

    def respond(prompt):
        if answer is None:
            raise EOFError
        return answer

    monkeypatch.setattr("builtins.input", respond)
    assert main(["confirm"]) == 1
    assert not (tmp_path / "data").exists()


def test_cli_confirm_saves_only_after_yes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("builtins.input", lambda prompt: "是")
    assert main(["confirm"]) == 0
    assert not first_run.UserAgreement(tmp_path / "data" / "user_agreement.json").status().needs_acceptance
