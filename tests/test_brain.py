"""``MuikaBrain`` LLM 层测试——用裸 ``FakeLLM`` stub 替换 ``brain.model``。

``MuikaBrain.__new__`` 绕过构造（避免 ``load_model`` / watcher 线程），
``generate_prompt_from_template`` 被 mock（不渲染真实模板）。
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from muika.core.brain import MuikaBrain
from muika.core.events import (
    SessionBootstrapEvent,
    TimeoutEvent,
    TimeTickEvent,
    UserMessageEvent,
    UserMessagePayload,
)
from muika.core.memory import MemoryCategory, MemoryLayer, MemoryManager, MemoryRecord
from muika.core.state import MuikaState
from muika.core.topic_manager import EventTopic, StaticTopic, TopicSource
from muika.ipc.server import AdapterInfo
from muika.llm import ModelCompletions
from muika.models import Message


def _user_event(msg: str = "hello") -> UserMessageEvent:
    return UserMessageEvent(payload=UserMessagePayload(message=Message(message=msg)))


def _brain(fake_llm) -> MuikaBrain:
    brain = MuikaBrain.__new__(MuikaBrain)
    brain.model = fake_llm
    brain._mcp_tools = []
    return brain


def _memory() -> MemoryManager:
    return MemoryManager()


# ---------------------------------------------------------------------------
# generate_adapters_info —— 静态方法
# ---------------------------------------------------------------------------


def test_adapters_info_none_or_small_none():
    assert MuikaBrain.generate_adapters_info(None) is None
    assert MuikaBrain.generate_adapters_info([]) is None
    assert MuikaBrain.generate_adapters_info([AdapterInfo(ws=None, client_name="qq")]) is None


def test_adapters_info_two_adapters():
    a1 = AdapterInfo(ws=None, client_name="qq", last_active_at=datetime.now() - timedelta(seconds=30))
    a2 = AdapterInfo(ws=None, client_name="telegram", last_active_at=datetime.now() - timedelta(hours=2))
    info = MuikaBrain.generate_adapters_info([a1, a2])
    assert "qq(Last active at just now)" in info
    assert "telegram(Last active at 2 hours ago)" in info


def test_adapters_info_ago_buckets():
    cases = [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=5), "5 min ago"),
        (timedelta(hours=2), "2 hours ago"),
        (timedelta(days=2), "2 days ago"),
    ]
    for delta, expected in cases:
        adapter = AdapterInfo(ws=None, client_name="x", last_active_at=datetime.now() - delta)
        info = MuikaBrain.generate_adapters_info([adapter, adapter])
        assert f"x(Last active at {expected})" in info


# ---------------------------------------------------------------------------
# generate_reply
# ---------------------------------------------------------------------------


async def test_generate_reply_user_message_prompt(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.generate_reply(_user_event(), MuikaState(), _memory())
    req = fake.requests[0]
    assert req.prompt.startswith("["), req.prompt
    assert req.prompt.endswith("] [User] hello"), req.prompt
    assert req.system == "SYSTEM"
    assert result == "Hi!"


async def test_generate_reply_strips_think(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi! <think>secret</think>"))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.generate_reply(_user_event(), MuikaState(), _memory())
    assert result == "Hi!"


async def test_generate_reply_builds_template_data(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    state = MuikaState()
    memory = _memory()
    memory.session.is_first_session = False
    memory.records["core:user:name"] = MemoryRecord(
        layer=MemoryLayer.CORE, category=MemoryCategory.USER, key="name", value="Alice"
    )
    pref = MemoryRecord(layer=MemoryLayer.PREFERENCE, category=MemoryCategory.USER, key="fav_drink", value="tea")
    captured = {}

    def _tmpl(name, data):
        captured["data"] = data
        return "SYSTEM"

    with patch("muika.core.brain.generate_prompt_from_template", side_effect=_tmpl):
        await brain.generate_reply(_user_event(), state, memory, injected_preferences=[pref])

    data = captured["data"]
    assert data.event_type == "user_message"
    assert data.is_chat is True
    assert data.memory_context == memory.get_memory_prompt()
    assert data.injected_preferences == [pref]
    assert data.adapters_info is None


async def test_generate_reply_session_bootstrap_first(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    captured = {}

    def _tmpl(name, data):
        captured["data"] = data
        return "SYSTEM"

    with patch("muika.core.brain.generate_prompt_from_template", side_effect=_tmpl):
        await brain.generate_reply(SessionBootstrapEvent(last_chat_time=None), MuikaState(), _memory())

    data = captured["data"]
    assert data.is_first_session is True
    assert data.absence_bucket == "short"
    assert data.last_connection_time is None


async def test_generate_reply_session_bootstrap_resume(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    memory = _memory()
    memory.session.is_first_session = False
    captured = {}

    def _tmpl(name, data):
        captured["data"] = data
        return "SYSTEM"

    with patch("muika.core.brain.generate_prompt_from_template", side_effect=_tmpl):
        await brain.generate_reply(
            SessionBootstrapEvent(last_chat_time=datetime.now() - timedelta(days=2)), MuikaState(), memory
        )

    data = captured["data"]
    assert data.is_first_session is False
    assert data.last_connection_time is not None


async def test_generate_reply_time_tick_lonely_prompt(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="..."))
    brain = _brain(fake)
    state = MuikaState(loneliness=0.9)  # mood 为派生属性，由孤独感驱动
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(TimeTickEvent(), state, _memory())
    req = fake.requests[0]
    assert "loneliness lingers" in req.prompt
    assert "[System]" in req.prompt


async def test_generate_reply_timeout_prompt(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="..."))
    brain = _brain(fake)
    event = TimeoutEvent(set_at=datetime.now() - timedelta(minutes=2), duration=300)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(event, MuikaState(), _memory())
    assert "5 minutes" in fake.requests[0].prompt


async def test_generate_reply_history_dedup(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    memory = _memory()
    memory.add_context("user", "hello")
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(_user_event("hello"), MuikaState(), memory)
    assert len(fake.requests[0].history) == 0


async def test_generate_reply_succeed_false_fallback(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="err", succeed=False))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.generate_reply(_user_event(), MuikaState(), _memory())
    assert result == "My mind feels foggy... I encountered an error."


async def test_generate_reply_exception_fallback(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.generate_reply(_user_event(), MuikaState(), _memory())
    assert result == "My mind feels foggy... I encountered an error."


async def test_generate_reply_god_mode_passes_tools(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)

    async def _tools():
        return [{"name": "t"}]

    brain._get_tool_list = _tools
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(_user_event(), MuikaState(), _memory(), god_mode=True)
    assert fake.requests[0].tools == [{"name": "t"}]


async def test_generate_reply_no_god_mode_no_tools(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(_user_event(), MuikaState(), _memory(), god_mode=False)
    assert fake.requests[0].tools is None


# ---------------------------------------------------------------------------
# expand_topic
# ---------------------------------------------------------------------------


async def test_expand_topic_static(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="The moon <think>hmm</think>"))
    brain = _brain(fake)
    topic = StaticTopic(id="a1", source=TopicSource.STATIC, category="trivia", content="the moon")
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.expand_topic(topic, MuikaState(), _memory())
    assert result == "The moon"
    assert "the moon" in fake.requests[0].prompt


async def test_expand_topic_event(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="ok"))
    brain = _brain(fake)
    topic = EventTopic(id="e1", source=TopicSource.EVENT, category="news", title="AI", content="article body")
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.expand_topic(topic, MuikaState(), _memory())
    assert "You've read an article about AI" in fake.requests[0].prompt


async def test_expand_topic_failure_empty(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    brain = _brain(fake)
    topic = StaticTopic(id="a1", source=TopicSource.STATIC, category="trivia", content="x")
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.expand_topic(topic, MuikaState(), _memory())
    assert result == ""
