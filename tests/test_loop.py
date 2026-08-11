"""``Muika._parse_reply_tags``（标签解析）与 ``get_think_mode``（认知管线选择）测试。

``get_think_mode`` 用 ``__new__`` 构造实例，绕开 ``__init__`` 触发的 LLM/DB 加载。
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from muika.core.events import TimeTickEvent, UserMessageEvent, UserMessagePayload
from muika.core.loop import Muika, ParsedReply
from muika.core.state import ActiveTopicState, MuikaState
from muika.models import Message

# ---------------------------------------------------------------------------
# _parse_reply_tags —— 静态纯方法
# ---------------------------------------------------------------------------


def test_parse_no_tags():
    r = Muika._parse_reply_tags("Hello there")
    assert r == ParsedReply(clean_reply="Hello there", memory_contents=[], agent_commands=[], target=None)


def test_parse_memory_tag():
    r = Muika._parse_reply_tags("<memory>She likes tea</memory>")
    assert r.memory_contents == ["She likes tea"]
    assert r.clean_reply == ""


def test_parse_multiple_memories():
    r = Muika._parse_reply_tags("<memory>one</memory> <memory>two</memory>")
    assert r.memory_contents == ["one", "two"]


def test_parse_memory_empty_stripped():
    r = Muika._parse_reply_tags("<memory>   </memory>")
    assert r.memory_contents == []


def test_parse_agent_command():
    r = Muika._parse_reply_tags("<agent> run cmd </agent>")
    assert r.agent_commands == ["run cmd"]
    assert r.clean_reply == ""


def test_parse_agent_case_insensitive():
    r = Muika._parse_reply_tags("<AGENT>xyz</AGENT>")
    assert r.agent_commands == ["xyz"]


def test_parse_target():
    r = Muika._parse_reply_tags("Hi <target: qq>")
    assert r.target == "qq"
    assert r.clean_reply == "Hi"


def test_parse_target_last_wins():
    r = Muika._parse_reply_tags("<target: a> <target: b>")
    assert r.target == "b"


def test_parse_timeout():
    assert Muika._parse_reply_tags("<timeout: 10min>").timeout == 600.0
    assert Muika._parse_reply_tags("<timeout: 2h>").timeout == 7200.0


def test_parse_timeout_unrecognized_ignored():
    r = Muika._parse_reply_tags("<timeout: someday>")
    assert r.timeout is None
    assert r.clean_reply == ""


def test_parse_god_mode_variants():
    assert Muika._parse_reply_tags("<enable_god_mode>").god_mode is True
    assert Muika._parse_reply_tags("<enable_god_mode/>").god_mode is True


def test_parse_combined():
    r = Muika._parse_reply_tags(
        "Hello <memory>m</memory> <agent>cmd</agent> <target: t> <timeout: 1h> <enable_god_mode>"
    )
    assert r == ParsedReply(
        clean_reply="Hello",
        memory_contents=["m"],
        agent_commands=["cmd"],
        target="t",
        timeout=3600.0,
        god_mode=True,
    )


# ---------------------------------------------------------------------------
# get_think_mode
# ---------------------------------------------------------------------------


def _engine() -> Muika:
    m = Muika.__new__(Muika)
    m.state = MuikaState()
    return m


def _user_event() -> UserMessageEvent:
    return UserMessageEvent(payload=UserMessagePayload(message=Message(message="hi")))


def _tick() -> TimeTickEvent:
    return TimeTickEvent()


def test_think_mode_user_message_emotional():
    assert _engine().get_think_mode(_user_event()) == "emotional"


def test_think_mode_idle_tick_none():
    assert _engine().get_think_mode(_tick()) is None


def test_think_mode_active_topic_blocks():
    m = _engine()
    m.state.active_topic = ActiveTopicState(topic_id="t", topic_seed="s", topic_type="trivia")
    assert m.get_think_mode(_tick()) is None


def test_think_mode_loneliness_emotional():
    m = _engine()
    m.state.loneliness = 0.9
    assert m.get_think_mode(_tick()) == "emotional"


def test_think_mode_loneliness_cooldown_blocks():
    m = _engine()
    m.state.loneliness = 0.9
    m.state.last_proactive_at = datetime.now() - timedelta(seconds=60)
    assert m.get_think_mode(_tick()) is None


def test_think_mode_boredom_topic():
    m = _engine()
    m.state.boredom = 0.61
    assert m.get_think_mode(_tick()) == "topic"


def test_think_mode_curiosity_topic():
    m = _engine()
    m.state.curiosity = 0.7
    with patch("muika.core.loop.random", return_value=0.0):
        assert m.get_think_mode(_tick()) == "topic"
    assert m.state.curiosity == 0.0


def test_think_mode_loneliness_overrides_boredom():
    m = _engine()
    m.state.loneliness = 0.9
    m.state.boredom = 0.9
    assert m.get_think_mode(_tick()) == "emotional"
