"""模板校验的 persona/agent 渲染语义分界。"""

from pathlib import Path

import pytest

from muika.core.self_mod.policy import SelfModError
from muika.core.self_mod.validators import validate_content, validate_template

REPO_ROOT = Path(__file__).resolve().parents[1]

# persona 数据下 is_chat=True 触发对 None 取长度；空上下文渲染时分支被跳过
GUARDED = "{% if is_chat %}{{ memory_context | length }}{% endif %}"


def _builtin(name: str) -> str:
    return (REPO_ROOT / "muika" / "builtin_templates" / name).read_text(encoding="utf-8")


def test_persona_template_must_render_with_prompt_data():
    with pytest.raises(SelfModError, match="prompt data"):
        validate_template(GUARDED)


def test_agent_template_renders_with_empty_context():
    validate_template(GUARDED, agent=True)


def test_builtin_templates_pass_their_own_semantics():
    validate_template(_builtin("Muika.md.jinja2"))
    validate_template(_builtin("Muika.agent.jinja2"), agent=True)


def test_agent_render_still_catches_literal_failures():
    with pytest.raises(SelfModError, match="empty context"):
        validate_template("{{ 1/0 }}", agent=True)


def test_syntax_error_rejected_for_both_kinds():
    for agent in (False, True):
        with pytest.raises(SelfModError, match="syntax"):
            validate_template("{% if %}", agent=agent)


def test_validate_content_dispatches_by_template_name(tmp_path):
    agent_path = tmp_path / "Muika.agent.jinja2"
    persona_path = tmp_path / "custom.md.jinja2"
    validate_content(agent_path, GUARDED)
    with pytest.raises(SelfModError):
        validate_content(persona_path, GUARDED)
