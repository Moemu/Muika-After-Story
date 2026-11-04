"""农历节日与公历节日日期（数据表 + 移动节日规则）。"""

from datetime import date

from muika.utils.lunar import festival_dates, lunar_to_solar


def test_lunar_to_solar() -> None:
    assert lunar_to_solar(2026, 1, 1) == date(2026, 2, 17)
    assert lunar_to_solar(2025, 8, 15) == date(2025, 10, 6)  # 闰六月
    assert lunar_to_solar(2025, 12, 8) == date(2026, 1, 26)
    assert lunar_to_solar(2026, 12, 29) == date(2027, 2, 5)


def test_chinese_new_year() -> None:
    expected = {
        2026: date(2026, 2, 17),
        2027: date(2027, 2, 6),
        2028: date(2028, 1, 26),
        2029: date(2029, 2, 13),
        2030: date(2030, 2, 3),
    }
    for year, day in expected.items():
        assert festival_dates(year)["春节"] == day


def test_lunar_festivals_within_gregorian_year() -> None:
    festivals = festival_dates(2026)
    assert festivals["元宵"] == date(2026, 3, 3)
    assert festivals["端午"] == date(2026, 6, 19)
    assert festivals["中秋"] == date(2026, 9, 25)
    assert festivals["重阳"] == date(2026, 10, 18)


def test_previous_lunar_year_festivals_land_in_january() -> None:
    # 2025 年农历腊月落在 2026 年 1-2 月
    festivals = festival_dates(2026)
    assert festivals["腊八"] == date(2026, 1, 26)
    assert festivals["小年"] == date(2026, 2, 10)
    assert festivals["除夕"] == date(2026, 2, 16)


def test_leap_month_year() -> None:
    # 2025 年闰六月，中秋落在公历 10 月
    assert festival_dates(2025)["中秋"] == date(2025, 10, 6)


def test_fixed_gregorian_holidays() -> None:
    festivals = festival_dates(2026)
    assert festivals["元旦"] == date(2026, 1, 1)
    assert festivals["情人节"] == date(2026, 2, 14)
    assert festivals["圣诞"] == date(2026, 12, 25)
    assert festivals["跨年"] == date(2026, 12, 31)


def test_movable_western_holidays() -> None:
    festivals = festival_dates(2026)
    assert festivals["母亲节"] == date(2026, 5, 10)
    assert festivals["父亲节"] == date(2026, 6, 21)
    assert festivals["感恩节"] == date(2026, 11, 26)


def test_qingming_solar_term() -> None:
    assert festival_dates(2025)["清明"] == date(2025, 4, 4)
    assert festival_dates(2026)["清明"] == date(2026, 4, 5)
    assert festival_dates(2024)["清明"] == date(2024, 4, 4)


def test_winter_solstice_solar_term() -> None:
    assert festival_dates(2025)["冬至"] == date(2025, 12, 21)
    assert festival_dates(2026)["冬至"] == date(2026, 12, 22)


def test_far_future_year_still_supported() -> None:
    festivals = festival_dates(2200)
    assert festivals["元旦"] == date(2200, 1, 1)
    assert "春节" in festivals
