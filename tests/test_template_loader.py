from pathlib import Path

from muika.template import loader


def test_missing_template_returns_empty_prompt(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])

    assert loader.generate_prompt_from_template("missing.jinja2") == ""


def test_template_render_failure_returns_empty_prompt(tmp_path: Path, monkeypatch):
    (tmp_path / "broken.jinja2").write_text("{{ 1 / 0 }}", encoding="utf-8")
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])

    assert loader.generate_prompt_from_template("broken.jinja2") == ""
