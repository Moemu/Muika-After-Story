"""``SessionBootstrapEvent.absence_bucket`` 缺席时间段计算测试。

显式传入真实玩家消息时间或未知时间，验证时间段边界。
"""

from datetime import datetime, timedelta

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
