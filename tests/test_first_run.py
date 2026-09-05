"""验证包内协议读取和既有同意记录的兼容性。"""

import json
from datetime import datetime
from unittest.mock import Mock

import pytest

from muika.utils import first_run


def test_agreement_uses_package_resource_from_another_working_directory(tmp_path):
    legacy_file = tmp_path / "configs" / "user_agreement.json"
    legacy_file.parent.mkdir()
    legacy_file.write_text('{"title":"old override","text":"old text","updated":"2099-01-01"}', encoding="utf-8")
    title, text, updated = first_run._load_agreement_content()
    assert title == first_run.AGREEMENT_TITLE
    assert text == first_run.AGREEMENT_TEXT
    assert updated == "2026-02-01"
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
        first_run._load_agreement_content()


def test_existing_consent_does_not_prompt_again(tmp_path, monkeypatch):
    path = tmp_path / "user_agreement.json"
    original = '{"has_agreed":true,"timestamp":"2026-03-01T12:00:00","version":"2026-02-01"}'
    path.write_text(original, encoding="utf-8")
    agreement = first_run.UserAgreement()
    agreement.storage_path = path
    prompt = Mock()
    monkeypatch.setattr(agreement, "prompt_for_agreement", prompt)
    agreement.check_first_run()
    prompt.assert_not_called()
    assert path.read_text(encoding="utf-8") == original
    assert agreement.agreement_state.has_agreed


def test_save_consent_creates_data_directory(tmp_path):
    agreement = first_run.UserAgreement()
    agreement.storage_path = tmp_path / "new" / "data" / "user_agreement.json"
    agreement.agreement_state = first_run.AgreementState(
        has_agreed=True, timestamp=datetime(2026, 3, 1, 12), version="2026-02-01"
    )
    agreement.save_agreement()
    assert json.loads(agreement.storage_path.read_text(encoding="utf-8")) == {
        "has_agreed": True,
        "timestamp": "2026-03-01T12:00:00",
        "version": "2026-02-01",
    }
