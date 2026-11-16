"""节日规则加载与到期判断（内置 + 用户覆盖 + 自定义）。"""

from datetime import date
from pathlib import Path

from muika.core.topic_manager import due_occasions, load_occasion_rules


def test_builtin_rules_loaded() -> None:
    rules = load_occasion_rules()
    names = {rule.name for rule in rules}
    assert "春节" in names
    assert "中秋" in names
    assert "圣诞" in names
    assert "母亲节" in names
    assert len(rules) >= 25
    assert len({rule.id for rule in rules}) == len(rules)


def test_due_occasions_on_festival_day() -> None:
    rules = load_occasion_rules()
    due = {rule.name for rule, _ in due_occasions(rules, date(2026, 2, 17))}
    assert "春节" in due

    due = {rule.name for rule, _ in due_occasions(rules, date(2026, 3, 3))}
    assert "元宵" in due


def test_due_occasions_window(tmp_path: Path) -> None:
    # 内置节日窗口收敛为当天（避免"节前注入 today"的假事实）
    assert "春节" not in {rule.name for rule, _ in due_occasions(load_occasion_rules(), date(2026, 2, 16))}

    user = tmp_path / "occasions.yml"
    user.write_text(
        "- id: custom_window\n"
        "  name: 自定义窗口\n"
        "  date: [2, 17]\n"
        "  window_days: 3\n"
        '  prompt: "Custom window."\n',
        encoding="utf-8",
    )
    rules = load_occasion_rules(user)
    # window_days=3：节前 1 天与节后 1 天都在窗口内
    assert "自定义窗口" in {rule.name for rule, _ in due_occasions(rules, date(2026, 2, 16))}
    assert "自定义窗口" in {rule.name for rule, _ in due_occasions(rules, date(2026, 2, 18))}
    assert "自定义窗口" not in {rule.name for rule, _ in due_occasions(rules, date(2026, 2, 20))}


def test_due_occasions_handles_lunar_festival_in_january() -> None:
    rules = load_occasion_rules()
    due = {rule.name for rule, _ in due_occasions(rules, date(2026, 1, 26))}
    assert "腊八" in due


def test_due_occasions_movable_holiday() -> None:
    rules = load_occasion_rules()
    due = {rule.name for rule, _ in due_occasions(rules, date(2026, 5, 10))}
    assert "母亲节" in due


def test_due_occasions_halloween_and_winter_solstice() -> None:
    rules = load_occasion_rules()
    assert "万圣节" in {rule.name for rule, _ in due_occasions(rules, date(2026, 10, 31))}
    assert "冬至" in {rule.name for rule, _ in due_occasions(rules, date(2026, 12, 22))}


def test_user_rule_overrides_builtin(tmp_path: Path) -> None:
    user = tmp_path / "occasions.yml"
    user.write_text(
        "- id: chinese_new_year\n" "  name: 春节\n" "  window_days: 5\n" '  prompt: "Custom spring festival prompt."\n',
        encoding="utf-8",
    )
    rules = load_occasion_rules(user)
    spring = next(rule for rule in rules if rule.id == "chinese_new_year")
    assert spring.window_days == 5
    assert spring.prompt == "Custom spring festival prompt."


def test_user_custom_rule(tmp_path: Path) -> None:
    user = tmp_path / "occasions.yml"
    user.write_text(
        "- id: custom_anniversary\n"
        "  name: 我们的纪念日\n"
        "  date: [11, 3]\n"
        "  window_days: 1\n"
        '  prompt: "It is our anniversary today."\n',
        encoding="utf-8",
    )
    rules = load_occasion_rules(user)
    due = [rule.name for rule, _ in due_occasions(rules, date(2026, 11, 3))]
    assert "我们的纪念日" in due


def test_custom_lunar_rule_handles_previous_lunar_year(tmp_path: Path) -> None:
    user = tmp_path / "occasions.yml"
    user.write_text(
        "- id: custom_lunar_anniversary\n"
        "  name: 农历纪念日\n"
        "  lunar: [12, 8]\n"
        "  window_days: 1\n"
        '  prompt: "It is our lunar anniversary today."\n',
        encoding="utf-8",
    )
    rules = load_occasion_rules(user)
    # 2026-01-26 属于农历 2025 年腊月初八
    due = [rule.name for rule, _ in due_occasions(rules, date(2026, 1, 26))]
    assert "农历纪念日" in due
