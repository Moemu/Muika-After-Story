"""农历节日与公历节日日期查询（基于 lunar-python）。"""

from __future__ import annotations

from datetime import date
from functools import lru_cache

from lunar_python import Lunar, LunarYear, Solar

FIXED_SOLAR: dict[str, tuple[int, int]] = {
    "元旦": (1, 1),
    "情人节": (2, 14),
    "妇女节": (3, 8),
    "愚人节": (4, 1),
    "劳动节": (5, 1),
    "儿童节": (6, 1),
    "教师节": (9, 10),
    "国庆": (10, 1),
    "万圣节": (10, 31),
    "平安夜": (12, 24),
    "圣诞": (12, 25),
    "跨年": (12, 31),
}
"""公历固定节日（月, 日）。"""

LUNAR_FESTIVALS: tuple[tuple[str, int, int], ...] = (
    ("春节", 1, 1),
    ("元宵", 1, 15),
    ("龙抬头", 2, 2),
    ("端午", 5, 5),
    ("七夕", 7, 7),
    ("中元", 7, 15),
    ("中秋", 8, 15),
    ("重阳", 9, 9),
    ("寒衣", 10, 1),
    ("下元", 10, 15),
    ("腊八", 12, 8),
    ("小年", 12, 23),
)
"""农历节日（名称, 月, 日）。"""


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    """返回某月第 *ordinal* 个星期 *weekday* 的日期（weekday 以周一为 0）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return date(year, month, 1 + offset + (ordinal - 1) * 7)


def movable_festival_dates(year: int) -> dict[str, date]:
    """返回公历 *year* 年内的移动日期节日（母亲节/父亲节/感恩节）。"""
    return {
        "母亲节": _nth_weekday(year, 5, 6, 2),
        "父亲节": _nth_weekday(year, 6, 6, 3),
        "感恩节": _nth_weekday(year, 11, 3, 4),
    }


def _lunar_day_count(lunar_year: int, month: int) -> int:
    """返回农历某月的天数（用于校验农历日期与计算除夕）。"""
    for item in LunarYear.fromYear(lunar_year).getMonths():
        if item.getYear() == lunar_year and item.getMonth() == month:
            return item.getDayCount()
    raise ValueError(f"lunar month not found: {lunar_year}-{month}")


def _try_lunar_to_solar(lunar_year: int, month: int, day: int) -> date | None:
    """把农历日期转为公历；超出 lunar-python 支持范围时返回 None。"""
    try:
        solar = Lunar.fromYmd(lunar_year, month, day).getSolar()
    except (ValueError, IndexError):
        return None
    return date(solar.getYear(), solar.getMonth(), solar.getDay())


def lunar_to_solar(lunar_year: int, month: int, day: int) -> date:
    """把农历日期转换为公历日期（非闰月）。"""
    if not 1 <= month <= 12:
        raise ValueError(f"lunar month out of range: {month}")
    last_day = _lunar_day_count(lunar_year, month)
    if not 1 <= day <= last_day:
        raise ValueError(f"lunar day out of range: {lunar_year}-{month}-{day}")
    result = _try_lunar_to_solar(lunar_year, month, day)
    if result is None:
        raise ValueError(f"lunar date out of supported range: {lunar_year}-{month}-{day}")
    return result


@lru_cache(maxsize=8)
def festival_dates(year: int) -> dict[str, date]:
    """返回公历 *year* 年内的节日日期（农历 + 公历固定 + 西方移动 + 清明）。

    农历腊月节日（腊八/小年/除夕）可能落在次年 1 月，按公历年份归入。
    返回的字典只读，调用方不得修改。
    """
    result = {name: date(year, month, day) for name, (month, day) in FIXED_SOLAR.items()}
    result.update(movable_festival_dates(year))
    for name, month, day in LUNAR_FESTIVALS:
        for lunar_year in (year, year - 1):
            when = _try_lunar_to_solar(lunar_year, month, day)
            if when is not None and when.year == year:
                result[name] = when
                break
    for lunar_year in (year, year - 1):
        try:
            last_day = _lunar_day_count(lunar_year, 12)
        except ValueError:
            break
        when = _try_lunar_to_solar(lunar_year, 12, last_day)
        if when is not None and when.year == year:
            result["除夕"] = when
            break
    for day in range(3, 7):
        if Solar.fromYmd(year, 4, day).getLunar().getJieQi() == "清明":
            result["清明"] = date(year, 4, day)
            break
    for day in range(20, 24):
        if Solar.fromYmd(year, 12, day).getLunar().getJieQi() == "冬至":
            result["冬至"] = date(year, 12, day)
            break
    return result
