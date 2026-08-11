"""``muika.utils.utils`` 的时长解析/格式化函数测试（纯函数）。"""

import pytest

from muika.utils.utils import format_duration, parse_duration


def test_parse_duration_single_units():
    assert parse_duration("10s") == 10.0
    assert parse_duration("5min") == 300.0
    assert parse_duration("2h") == 7200.0
    assert parse_duration("1d") == 86400.0


def test_parse_duration_plural_and_spaces():
    assert parse_duration("3days") == 259200.0
    assert parse_duration("10 mins") == 600.0
    assert parse_duration(" 5min ") == 300.0


def test_parse_duration_decimal():
    assert parse_duration("1.5h") == 5400.0


def test_parse_duration_invalid():
    assert parse_duration("") is None
    assert parse_duration("10") is None
    assert parse_duration("xyz") is None
    assert parse_duration("1.5") is None


def test_parse_duration_milliseconds():
    # 回归：ms 不能被复数规则误剥成 m（分钟）
    assert parse_duration("100ms") == pytest.approx(0.1)
    assert parse_duration("5ms") == pytest.approx(0.005)
    assert parse_duration("1s") == pytest.approx(1.0)


def test_format_duration_seconds():
    assert format_duration(0) == "0 seconds"
    assert format_duration(30) == "30 seconds"
    assert format_duration(1) == "1 second"


def test_format_duration_minutes_hours_days():
    assert format_duration(90) == "2 minutes"
    assert format_duration(3600) == "1 hour"
    assert format_duration(86400) == "1 day"
    assert format_duration(172800) == "2 days"


def test_format_duration_negative_clamped():
    assert format_duration(-5) == "0 seconds"
