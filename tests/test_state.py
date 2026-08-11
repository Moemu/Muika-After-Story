"""``MuikaState.tick_state`` 状态机单元测试（零依赖）。"""

import pytest

from muika.core.events import TimeTickEvent, UserMessageEvent, UserMessagePayload
from muika.core.state import ActiveTopicState, MuikaState
from muika.models import Message


def _user_event(msg: str = "hi") -> UserMessageEvent:
    return UserMessageEvent(payload=UserMessagePayload(message=Message(message=msg)))


def _time_tick() -> TimeTickEvent:
    return TimeTickEvent()


def test_attention_decays_and_floors_at_zero():
    s = MuikaState()
    s.tick_state(_time_tick(), 1.0)
    assert s.attention == pytest.approx(0.95)
    for _ in range(30):
        s.tick_state(_time_tick(), 1.0)
    assert s.attention == 0.0


def test_loneliness_grows_capped_at_one():
    s = MuikaState()
    s.tick_state(_time_tick(), 10800 * 2)  # 6 小时 → 涨满并封顶
    assert s.loneliness == pytest.approx(1.0)


def test_boredom_grows_capped_at_one():
    s = MuikaState()
    s.tick_state(_time_tick(), 7200 * 2)  # 4 小时 → 涨满并封顶
    assert s.boredom == pytest.approx(1.0)


def test_curiosity_decays_by_factor():
    s = MuikaState(curiosity=0.5)
    s.tick_state(_time_tick(), 1.0)
    assert s.curiosity == pytest.approx(0.5 * 0.99)


def test_mood_calm_by_default():
    s = MuikaState()
    s.tick_state(_time_tick(), 1.0)
    assert s.mood == "calm"


def test_mood_lonely_when_loneliness_above_threshold():
    s = MuikaState(loneliness=0.8)
    s.tick_state(_time_tick(), 1080)  # +0.1 → 0.9 > 0.8
    assert s.mood == "lonely"


def test_mood_bored_when_boredom_above_threshold():
    s = MuikaState(boredom=0.65)
    s.tick_state(_time_tick(), 400)  # +0.0556 → 0.7056 > 0.7
    assert s.mood == "bored"


def test_lonely_takes_precedence_over_bored():
    s = MuikaState(loneliness=0.9, boredom=0.9)
    s.tick_state(_time_tick(), 0.0)
    assert s.mood == "lonely"


def test_user_message_resets_loneliness_and_attention():
    s = MuikaState(loneliness=0.7, attention=0.3)
    s.tick_state(_user_event(), 0.0)
    assert s.loneliness == 0.0
    assert s.attention == 1.0
    assert s.mood == "calm"


def test_user_message_resets_mood_to_calm():
    # 回归：情绪须依据用户消息重置后的最终状态判定，孤独感归零后不能残留 lonely
    s = MuikaState(loneliness=0.9)
    s.tick_state(_user_event(), 0.0)
    assert s.loneliness == 0.0
    assert s.mood == "calm"


def test_user_message_keeps_bored_mood_when_boredom_high():
    # boredom 不会被用户消息重置，仍按最终值判定
    s = MuikaState(boredom=0.9)
    s.tick_state(_user_event(), 0.0)
    assert s.mood == "bored"


def test_user_message_updates_last_interaction():
    s = MuikaState()
    before = s.last_interaction
    s.tick_state(_user_event(), 0.0)
    assert s.last_interaction >= before


def test_user_message_marks_active_topic_engaged():
    s = MuikaState(active_topic=ActiveTopicState(topic_id="t1", topic_seed="seed", topic_type="trivia"))
    s.tick_state(_user_event(), 0.0)
    assert s.active_topic is not None
    assert s.active_topic.user_engaged is True


def test_non_user_event_does_not_reset():
    s = MuikaState(loneliness=0.7, attention=0.3)
    last_interaction = s.last_interaction
    s.tick_state(_time_tick(), 0.0)
    assert s.loneliness == pytest.approx(0.7)  # 不归零，仅 +0
    assert s.attention == pytest.approx(0.25)  # 注意力照常衰减
    assert s.last_interaction == last_interaction
