from pathlib import Path

import pytest
from jinja2 import TemplateNotFound

from muika.config import mas_config
from muika.core.actions.tools._self_edit import persona_switch
from muika.core.state import MuikaState
from muika.template import loader
from muika.template.model import PromptTemplatesData


def test_prompt_renders_current_derived_mood(tmp_path: Path, monkeypatch):
    (tmp_path / "mood.jinja2").write_text("Mood: {{ state.mood }}", encoding="utf-8")
    monkeypatch.setattr(loader, "SEARCH_PATH", [tmp_path])
    state = MuikaState(loneliness=0.9, boredom=0.9)
    data = PromptTemplatesData(event_type="user_message", state=state, is_chat=True)

    assert loader.generate_prompt_from_template("mood.jinja2", data) == "Mood: lonely"
    state.loneliness = 0.0
    assert loader.generate_prompt_from_template("mood.jinja2", data) == "Mood: bored"
    state.boredom = 0.0
    assert loader.generate_prompt_from_template("mood.jinja2", data) == "Mood: calm"


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


def test_medium_heartbeat_renders_relaxed_direct_question_rule():
    """合并回归：两个 medium 分支时，后一个（放宽必答句）成为死代码。"""
    data = PromptTemplatesData(
        event_type="user_message",
        state=MuikaState(),
        is_chat=True,
        heartbeat_intensity="medium",
    )

    prompt = loader.generate_prompt_from_template("Muika.md.jinja2", data)

    assert "A direct question is normally answered." in prompt
    assert "A direct question from the user is always answered." not in prompt
    # 精简：medium 与 low/high 同构，只描述思考方式，不重复总述里的 heart/do_nothing 规则。
    assert "Let the introspection improve the reply" not in prompt
    assert "The <heart> block is strictly private" not in prompt


def test_persona_template_has_single_medium_heartbeat_branch():
    """结构回归：同一模板里不应出现两个 medium 分支。"""
    template = (loader._BUILTIN_TEMPLATES_DIR / "Muika.md.jinja2").read_text(encoding="utf-8")

    assert template.count('heartbeat_intensity == "medium"') == 1


@pytest.mark.asyncio
async def test_persona_switch_respects_self_modification_switch(tmp_path: Path, monkeypatch):
    original_template = mas_config.persona_template
    monkeypatch.setattr(mas_config, "action_permission", "write")

    result = await persona_switch("missing.jinja2")

    assert result == "Self-modification is disabled by configuration."
    assert mas_config.persona_template == original_template
    assert not (tmp_path / ".env").exists()
