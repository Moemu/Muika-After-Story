"""``MuikaBrain`` LLM 层测试——用裸 ``FakeLLM`` stub 替换 ``brain.model``。

``MuikaBrain.__new__`` 绕过构造（避免 ``load_model`` / watcher 线程），
``generate_prompt_from_template`` 被 mock（不渲染真实模板）。
"""

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

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
from muika.llm import ModelCompletions
from muika.models import AdapterInfo, Message


def _user_event(msg: str = "hello") -> UserMessageEvent:
    return UserMessageEvent(payload=UserMessagePayload(message=Message(message=msg)))


def _brain(fake_llm) -> MuikaBrain:
    brain = MuikaBrain.__new__(MuikaBrain)
    brain.model = fake_llm
    return brain


def _memory() -> MemoryManager:
    return MemoryManager()


@pytest.fixture(autouse=True)
def model_config_manager():
    """将 ``get_model_config_manager`` 替换为带 heart_intensity 的轻量替身。

    ``generate_reply`` 会读取 Heart 强度；真实管理器会在测试中拉起文件 watcher，
    因此统一 stub 掉，避免加载 models.yml / 启动 Observer。
    """
    from types import SimpleNamespace

    stub = SimpleNamespace(heart_intensity="medium")
    with patch("muika.core.brain.get_model_config_manager", return_value=stub):
        yield stub


# ---------------------------------------------------------------------------
# generate_adapters_info —— 静态方法
# ---------------------------------------------------------------------------


def test_adapters_info_none_or_small_none():
    assert MuikaBrain.generate_adapters_info(None) is None
    assert MuikaBrain.generate_adapters_info([]) is None
    assert MuikaBrain.generate_adapters_info([AdapterInfo(client_name="qq")]) is None


def test_adapters_info_two_adapters():
    a1 = AdapterInfo(client_name="qq", last_active_at=datetime.now() - timedelta(seconds=30))
    a2 = AdapterInfo(client_name="telegram", last_active_at=datetime.now() - timedelta(hours=2))
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
        adapter = AdapterInfo(client_name="x", last_active_at=datetime.now() - delta)
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


async def test_generate_reply_accepts_fixed_clock(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    fixed = datetime.fromisoformat("2026-08-14T12:34:56+08:00")
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        await brain.generate_reply(_user_event(), MuikaState(), _memory(), now=fixed)
    assert fake.requests[0].prompt.startswith("[2026-08-14 12:34:56]")


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


async def test_generate_reply_heartbeat_intensity_in_template(fake_llm_factory, model_config_manager):
    model_config_manager.heart_intensity = "high"
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    captured = {}

    def _tmpl(name, data):
        captured["data"] = data
        return "SYSTEM"

    with patch("muika.core.brain.generate_prompt_from_template", side_effect=_tmpl):
        await brain.generate_reply(_user_event(), MuikaState(), _memory())

    assert captured["data"].heartbeat_intensity == "high"


async def test_generate_reply_heartbeat_intensity_off(fake_llm_factory, model_config_manager):
    model_config_manager.heart_intensity = "off"
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)
    captured = {}

    def _tmpl(name, data):
        captured["data"] = data
        return "SYSTEM"

    with patch("muika.core.brain.generate_prompt_from_template", side_effect=_tmpl):
        await brain.generate_reply(_user_event(), MuikaState(), _memory())

    assert captured["data"].heartbeat_intensity == "off"


async def test_generate_reply_keeps_heart_block(fake_llm_factory, model_config_manager):
    """heart 是私有内心独白，brain 不剥离——由 loop 的 _parse_reply_tags 从显示文本剥离。"""
    fake = fake_llm_factory(response=ModelCompletions(text="<heart>secret</heart>Hello"))
    brain = _brain(fake)
    with patch("muika.core.brain.generate_prompt_from_template", return_value="SYSTEM"):
        result = await brain.generate_reply(_user_event(), MuikaState(), _memory())
    assert result == "<heart>secret</heart>Hello"


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


async def test_generate_reply_god_mode_passes_tools(fake_llm_factory, monkeypatch):
    fake = fake_llm_factory(response=ModelCompletions(text="Hi!"))
    brain = _brain(fake)

    def _tools():
        return [{"name": "t"}]

    monkeypatch.setattr("muika.core.brain.get_tool_list", _tools)
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


async def test_mcp_tools_remain_in_consecutive_requests_and_clear_on_cleanup(fake_llm_factory, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from muika.plugin.mcp import client

    tool = SimpleNamespace(name="remote_probe", description="probe", input_schema={"type": "object"})
    server = SimpleNamespace(
        name="test", initialize=AsyncMock(), list_tools=AsyncMock(return_value=[tool]), cleanup=AsyncMock()
    )
    monkeypatch.setattr(client, "_servers", [])
    monkeypatch.setattr(client, "_tools", [])
    monkeypatch.setattr(client, "get_mcp_server_config", lambda: {"test": {}})
    monkeypatch.setattr(client, "Server", lambda *args: server)
    fake = fake_llm_factory(response=ModelCompletions(text="hello"))
    brain = _brain(fake)
    try:
        await client.initialize_servers()
        for _ in range(2):
            await brain.generate_reply(_user_event(), MuikaState(), _memory(), god_mode=True)
        assert all("remote_probe" in {t["function"]["name"] for t in r.tools} for r in fake.requests)
        server.list_tools.assert_awaited_once()
        await client.cleanup_servers()
        await brain.generate_reply(_user_event(), MuikaState(), _memory(), god_mode=True)
        assert "remote_probe" not in {t["function"]["name"] for t in fake.requests[-1].tools}
    finally:
        await client.cleanup_servers()
