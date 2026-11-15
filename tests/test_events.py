"""``SessionBootstrapEvent.absence_bucket`` 缺席时间段计算测试。

显式传入真实玩家消息时间或未知时间，验证时间段边界。
"""

from datetime import datetime, timedelta

import pytest

from muika.core.events import SessionBootstrapEvent


def _boot(last_chat_time):
    return SessionBootstrapEvent(last_chat_time=last_chat_time)


def test_absence_bucket_no_last_chat_short():
    assert _boot(None).absence_bucket == "short"


def test_absence_bucket_under_three_hours_short():
    assert _boot(datetime.now() - timedelta(hours=1)).absence_bucket == "short"


def test_absence_bucket_under_one_day_medium():
    assert _boot(datetime.now() - timedelta(hours=5)).absence_bucket == "medium"


def test_absence_bucket_over_one_day_long():
    assert _boot(datetime.now() - timedelta(days=2)).absence_bucket == "long"


@pytest.mark.parametrize(
    "gap, expected",
    [
        (timedelta(hours=1), "short"),
        (timedelta(hours=3), "medium"),
        (timedelta(days=1) - timedelta(microseconds=1), "medium"),
        (timedelta(days=1), "long"),
    ],
)
def test_historical_bootstrap_uses_event_timestamp(gap, expected):
    timestamp = datetime(2020, 1, 2)
    event = SessionBootstrapEvent(timestamp=timestamp, last_chat_time=timestamp - gap)
    assert event.absence_bucket == expected
