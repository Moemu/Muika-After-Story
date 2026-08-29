from pathlib import Path

import pytest
from jinja2 import TemplateNotFound

from muika.template import loader


def test_missing_template_raises(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])

    with pytest.raises(TemplateNotFound):
        loader.generate_prompt_from_template("missing.jinja2")


def test_template_render_failure_raises(tmp_path: Path, monkeypatch):
    (tmp_path / "broken.jinja2").write_text("{{ 1 / 0 }}", encoding="utf-8")
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])

    with pytest.raises(RuntimeError, match="Template render failed"):
        loader.generate_prompt_from_template("broken.jinja2")


def test_template_configuration_fails_before_startup(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])

    with pytest.raises(RuntimeError, match="Invalid template configuration"):
        loader.validate_template_configuration(["missing.jinja2"])
