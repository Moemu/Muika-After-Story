"""验证素材留存、日记事务、事实权重和持续状态。"""

import math
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from muika.core.memory import (
    PROMPT_WORKING_SUMMARY_CHARS,
    MemoryCategory,
    MemoryManager,
    MemoryQuery,
    StateUpdate,
    _visible_working_summary,
)
from muika.core.memory_models import (
    PROMPT_LASTING_TOKENS,
    DreamResult,
    Fact,
    FactUpdate,
    Intention,
    PersistentState,
)
from muika.database.orm_models import (
    ArchiveRecordORM,
    DiaryORM,
    ExperienceORM,
    FactORM,
    FactRecallORM,
    MemoryRecordORM,
)
from muika.llm import ModelCompletions, ModelConfig, ModelRequest
from muika.llm.context import ContextCompactor, ContextOverflowWarning, estimate_tokens
from muika.models import Resource


@pytest.fixture(autouse=True)
def database(redirect_get_session):
    return redirect_get_session


async def test_raw_dialogue_survives_cache_size_and_restart():
    memory = MemoryManager()
    await memory.load()
    start = datetime(2026, 9, 1, 12)
    for index in range(145):
        await memory.add_context("user", f"第 {index} 句原话", timestamp=start + timedelta(minutes=index))
    restored = MemoryManager()
    await restored.load()
    assert not restored.recent_turns
    matches = await restored.search(MemoryQuery(terms=["第 17 句"]))
    assert len(matches) == 1
    original = await restored.read_source(matches[0].ref)
    assert "第 16 句原话" in original and "第 18 句原话" in original
    assert "2026-09-01 12:17:00" in original
    assert not restored.session.is_first_session


async def test_new_sessions_share_a_day_and_missed_empty_days_are_skipped():
    memory = MemoryManager()
    await memory.add_context("user", "Morning", timestamp=datetime(2026, 9, 1, 9))
    await memory.new_session()
    await memory.add_context("muika", "A poem I wrote", timestamp=datetime(2026, 9, 1, 20))
    await memory.add_context("user", "Return", timestamp=datetime(2026, 9, 4, 20))
    assert len(await memory.day_material(date(2026, 9, 1))) == 2
    assert await memory.pending_days(datetime(2026, 9, 5, 4)) == [date(2026, 9, 1)]
    assert await memory.pending_days(datetime(2026, 9, 5, 5)) == [date(2026, 9, 1), date(2026, 9, 4)]


async def _first_fact(memory, day=date(2026, 9, 1)):
    ref = await memory.add_context("user", "Alice likes tea.", timestamp=datetime.combine(day, datetime.min.time()))
    update = FactUpdate(
        category=MemoryCategory.USER, key="master.drink", value="tea", source_refs=[f"experience:{ref}"]
    )
    result = DreamResult(diary="I remembered Alice's tea as I wrote my poem.", facts=[update])
    await memory.save_dream(day, result, ref, {f"experience:{ref}"})
    return next(iter(memory.facts.values())), ref, result


async def test_dream_retry_and_duplicate_mentions_count_once(database):
    memory = MemoryManager()
    fact, ref, result = await _first_fact(memory)
    weight = fact.weight
    assert not await memory.save_dream(date(2026, 9, 1), result, ref, {f"experience:{ref}"})
    late = await memory.add_material("note", "I thought of tea again.", timestamp=datetime(2026, 9, 1, 22))
    repeated = DreamResult(diary="I remembered that same tea again.", recalled_fact_ids=[fact.id, fact.id])
    await memory.save_dream(date(2026, 9, 1), repeated, late, {f"experience:{late}", f"fact:{fact.id}"})
    assert memory.facts[fact.id].weight == weight
    assert await database.scalar(select(func.count()).select_from(DiaryORM)) == 1
    assert await database.scalar(select(func.count()).select_from(FactRecallORM)) == 1
    assert await memory.pending_days(datetime(2026, 9, 2, 5)) == []


async def test_background_fact_input_does_not_gain_weight():
    memory = MemoryManager()
    fact, _, _ = await _first_fact(memory)
    ref = await memory.add_context("muika", "I explored a new poem.", timestamp=datetime(2026, 9, 2, 18))
    await memory.save_dream(
        date(2026, 9, 2),
        DreamResult(diary="The poem raised a question of my own."),
        ref,
        {f"experience:{ref}", f"fact:{fact.id}"},
    )
    assert memory.facts[fact.id].weight == fact.weight


async def test_failed_dream_rolls_back_every_part(database, monkeypatch):
    memory = MemoryManager()
    fact, _, _ = await _first_fact(memory)
    ref = await memory.add_context("user", "I now prefer coffee", timestamp=datetime(2026, 9, 2, 18))
    result = DreamResult(
        diary="A change.",
        facts=[
            FactUpdate(
                category=MemoryCategory.USER, key="master.drink", value="coffee", source_refs=[f"experience:{ref}"]
            )
        ],
    )
    old_snapshot = memory.snapshot
    monkeypatch.setattr(memory, "_save_snapshot", AsyncMock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        await memory.save_dream(date(2026, 9, 2), result, ref, {f"experience:{ref}"})
    assert memory.snapshot is old_snapshot and memory.facts[fact.id].value == "tea"
    assert await database.scalar(select(func.count()).select_from(DiaryORM)) == 1
    assert await database.scalar(select(func.count()).select_from(FactORM)) == 1
    assert await memory.pending_days(datetime(2026, 9, 3, 5)) == [date(2026, 9, 2)]


async def test_corrections_and_distinct_subjects_keep_separate_versions(database):
    memory = MemoryManager()
    old, _, _ = await _first_fact(memory)
    ref = await memory.add_context(
        "user", "I prefer coffee now. The other Alice likes tea.", timestamp=datetime(2026, 9, 2, 18)
    )
    refs = [f"experience:{ref}"]
    result = DreamResult(
        diary="Two different tastes.",
        facts=[
            FactUpdate(category=MemoryCategory.USER, key="master.drink", value="coffee", source_refs=refs),
            FactUpdate(category=MemoryCategory.USER, key="neighbor_alice.drink", value="tea", source_refs=refs),
        ],
    )
    await memory.save_dream(date(2026, 9, 2), result, ref, set(refs))
    assert old.id not in memory.facts
    original = await memory.read_source(f"fact:{old.id}")
    assert "Superseded or invalid fact" in original and "tea" in original
    assert {f.key: f.value for f in memory.facts.values()} == {"master.drink": "coffee", "neighbor_alice.drink": "tea"}
    assert await database.scalar(select(func.count()).select_from(FactORM)) == 3
    await memory.forget_memory(MemoryCategory.USER, "master.drink")
    prompt = memory.get_memory_prompt()
    assert "coffee" not in prompt
    assert "tea" in prompt
    assert "[fact:" not in prompt


def test_weight_decay_and_resident_budget():
    now = datetime.now()
    memory = MemoryManager()
    for index in range(25):
        memory.facts[index] = Fact(
            id=index,
            category=MemoryCategory.SELF,
            key=f"interest{index}",
            value="poetry",
            weight=index + 1,
            weight_at=now,
        )
    old = Fact(
        id=30,
        category=MemoryCategory.USER,
        key="old",
        value="long ago",
        weight=100,
        weight_at=now - timedelta(days=900),
    )
    assert old.score(now) == pytest.approx(100 / 1024)
    memory.facts[30] = old
    prompt = memory.get_memory_prompt()
    assert len(prompt.splitlines()) == 20
    assert "long ago" not in prompt
    assert estimate_tokens(memory.get_memory_prompt(budget=100)) <= 100
    memory.snapshot.first_interaction_at = datetime(2025, 1, 1, 12)
    prompt = memory.get_memory_prompt()
    assert "[Relationship history] Earliest known interaction with Master: 2025-01-01 12:00:00" in prompt
    assert len(prompt.splitlines()) == 21
    assert estimate_tokens(memory.get_memory_prompt(budget=100)) <= 100
    assert memory.get_memory_prompt(budget=1) == ""


async def test_lasting_anger_survives_sessions_restart_and_old_dream():
    memory = MemoryManager()
    await memory.update_state(StateUpdate(mood="angry", reason="A promise was broken"))
    await memory.new_session()
    restored = MemoryManager()
    await restored.load()
    assert restored.persistent.mood == "angry"
    await restored.update_state(StateUpdate(mood="hurt, but hopeful", reason="He apologized"))
    day = date.today() - timedelta(days=1)
    ref = await restored.add_context("user", "Yesterday", timestamp=datetime.combine(day, datetime.min.time()))
    await restored.save_dream(
        day,
        DreamResult(
            diary="Yesterday was painful.", state_update=StateUpdate(mood="angry", reason="Yesterday's promise")
        ),
        ref,
        {f"experience:{ref}"},
    )
    assert restored.persistent.mood == "hurt, but hopeful"


async def test_action_relief_is_small_and_task_link_is_idempotent():
    memory = MemoryManager()
    memory.persistent.dissonance = 0.8
    await memory.update_state(
        StateUpdate(reason="I want to learn", intentions=[Intention(id="poem", description="Read a poem")])
    )
    await memory.link_intention("poem", "task1")
    await memory.link_intention("poem", "task1")
    with pytest.raises(ValueError):
        await memory.link_intention("poem", "task2")
    await memory.record_task_result("task1", "completed")
    assert memory.persistent.intentions[0].status == "awaiting_feedback"
    day = date.today() - timedelta(days=1)
    ref = await memory.add_context("agent", "Read the poem", timestamp=datetime.combine(day, datetime.min.time()))
    await memory.save_dream(
        day,
        DreamResult(
            diary="I read it; I still wonder what he will think.",
            dissonance_delta=-0.5,
            tension_reason="I acted, without feedback",
            tension_source_refs=[f"experience:{ref}"],
            relief="action_without_feedback",
        ),
        ref,
        {f"experience:{ref}"},
    )
    assert memory.persistent.dissonance == pytest.approx(0.75)


@pytest.mark.parametrize("intention_status", ["resolved", "abandoned"])
@pytest.mark.parametrize("task_status", ["completed", "cancelled", "failed"])
async def test_task_result_preserves_closed_intention_and_explicit_reopening(intention_status, task_status):
    memory = MemoryManager()
    intention = Intention(id="poem", description="Read a poem")
    await memory.update_state(StateUpdate(reason="I want to read", intentions=[intention]))
    await memory.link_intention("poem", "task1")
    intention.status = intention_status
    await memory.update_state(StateUpdate(reason="I changed my mind", intentions=[intention]))
    closed = memory.persistent.intentions[0].model_copy(deep=True)
    await memory.record_task_result("task1", task_status)
    restored = MemoryManager()
    await restored.load()
    assert restored.persistent.intentions[0] == closed
    assert closed.task_id == "task1"
    intention.status = "open"
    await restored.update_state(StateUpdate(reason="I want to revisit it", intentions=[intention]))
    await restored.load()
    assert restored.persistent.intentions[0].status == "open"
    assert restored.persistent.intentions[0].task_id == "task1"


@pytest.mark.parametrize("known_first_conversation", [False, True])
async def test_legacy_material_keeps_provenance_and_runtime_metadata(database, known_first_conversation):
    for layer, category, key, value in [
        ("core", "user", "name", "Alice"),
        ("preference", "self", "book", "Poems"),
        ("state", "relation", "mood", "Old anger"),
        ("core", "self", "first_conversation_time", "2025-01-01T12:00:00"),
    ]:
        if key == "first_conversation_time" and not known_first_conversation:
            continue
        database.add(
            MemoryRecordORM(
                layer=layer,
                category=category,
                key=key,
                value=value,
                created_at="2025-01-01T12:00:00",
                updated_at="2025-01-01T12:00:00",
            )
        )
    database.add(
        ArchiveRecordORM(
            session_id="old",
            summary="A legacy session",
            period_start="2025-01-01T12:00:00",
            period_end="2025-01-01T13:00:00",
            created_at="2025-01-01T13:00:00",
        )
    )
    await database.commit()
    memory = MemoryManager()
    await memory.load()
    assert len(memory.facts) == 2 and {fact.weight for fact in memory.facts.values()} == {1.0}
    assert memory.persistent.mood == ""
    assert memory.snapshot.first_interaction_at == datetime(2025, 1, 1, 12)
    diary = (await memory.recent_diaries(date.today()))[0]
    assert diary.source.startswith("legacy_archive:")
    assert await memory.pending_days(datetime.now()) == []
    await memory.load()
    assert await database.scalar(select(func.count()).select_from(ExperienceORM)) == 3
    restored = MemoryManager()
    await restored.load()
    assert (
        "[Relationship history] Earliest known interaction with Master: 2025-01-01 12:00:00"
        in restored.get_memory_prompt()
    )


def test_unknown_relationship_date_is_not_invented():
    assert MemoryManager().get_memory_prompt() == ""


async def test_resources_are_immutable_and_private_tags_never_recalled(tmp_path):
    path = tmp_path / "capture.txt"
    path.write_text("original")
    memory = MemoryManager()
    ref = await memory.add_context(
        "muika", "<heart>private</heart>Hello<state>{}</state>", [Resource(type="file", path=str(path))]
    )
    path.write_text("changed")
    assert memory.recent_turns[-1].resources[0].path != str(path)
    assert "private" not in await memory.read_source(f"experience:{ref}")
    assert "<state>" not in await memory.read_source(f"experience:{ref}")
    assert memory.recent_turns[-1].content == "Hello"


async def test_context_summary_commits_before_replacing_history(monkeypatch):
    memory = MemoryManager()
    for index in range(12):
        await memory.add_context("user" if index % 2 == 0 else "muika", f"Turn {index}: " + "long thought " * 200)
    config = ModelConfig(provider="_echo", context_window=8192, max_tokens=1024)
    compactor = AsyncMock(spec=ContextCompactor)
    compactor.summarize.return_value = "Decisions, unfinished wishes and experience:1."
    request = ModelRequest(prompt="The current request", system="Muika", history=list(memory.recent_turns))
    before = list(memory.recent_turns)
    save = memory._save_snapshot
    monkeypatch.setattr(memory, "_save_snapshot", AsyncMock(side_effect=OSError("checkpoint failed")))
    model = SimpleNamespace(config=config, compactor=compactor)
    with pytest.raises(OSError):
        await memory.prepare_context(model, request, [])
    assert list(memory.recent_turns) == before and memory.snapshot.working_summary == ""
    monkeypatch.setattr(memory, "_save_snapshot", save)
    prepared, _ = await memory.prepare_context(model, request, [])
    assert prepared.prompt == "The current request" and len(prepared.history) < len(before)
    restored = MemoryManager()
    await restored.load()
    assert restored.snapshot.working_summary == restored.snapshot.latest_dialogue_summary
    assert not restored.recent_turns
    assert "Turn 0:" in await restored.read_source("experience:1")


async def test_current_request_is_never_silently_truncated():
    memory = MemoryManager()
    request = ModelRequest(prompt="required current input " * 2000)
    config = ModelConfig(provider="_echo", context_window=4096, max_tokens=1024)
    with pytest.warns(ContextOverflowWarning):
        prepared, _ = await memory.prepare_context(
            SimpleNamespace(config=config, compactor=AsyncMock(spec=ContextCompactor)), request, []
        )
    assert prepared is request
    assert request.prompt.endswith("required current input ")


async def test_late_old_evidence_does_not_replace_newer_fact():
    memory = MemoryManager()
    latest, _, _ = await _first_fact(memory, date(2026, 9, 3))
    ref = await memory.add_context("user", "I used to prefer coffee.", timestamp=datetime(2026, 9, 1, 12))
    await memory.save_dream(
        date(2026, 9, 1),
        DreamResult(
            diary="An older preference.",
            facts=[
                FactUpdate(
                    category=MemoryCategory.USER, key="master.drink", value="coffee", source_refs=[f"experience:{ref}"]
                )
            ],
        ),
        ref,
        {f"experience:{ref}"},
    )
    assert list(memory.facts) == [latest.id]
    assert memory.facts[latest.id].value == "tea"


async def test_same_day_dream_preserves_later_mood_and_handles_new_feedback():
    memory = MemoryManager()
    memory.persistent.dissonance = 0.8
    ref = await memory.add_context("agent", "Read my poem.", timestamp=datetime.now() - timedelta(minutes=1))
    await memory.update_state(StateUpdate(mood="hopeful", reason="He just thanked me"))
    action = DreamResult(
        diary="I read it and waited.",
        state_update=StateUpdate(mood="anxious", reason="Waiting earlier"),
        dissonance_delta=-0.3,
        tension_reason="I acted",
        tension_source_refs=[f"experience:{ref}"],
        relief="action_without_feedback",
    )
    await memory.save_dream(date.today(), action, ref, {f"experience:{ref}"})
    assert memory.persistent.mood == "hopeful"
    assert memory.persistent.dissonance == pytest.approx(0.75)
    feedback = await memory.add_context("user", "I liked your poem. Please share another.")
    result = DreamResult(
        diary="He enjoyed it, and I want to write again.",
        dissonance_delta=-0.2,
        tension_reason="He enjoyed the poem",
        tension_source_refs=[f"experience:{feedback}"],
        relief="positive_feedback",
    )
    assert await memory.save_dream(date.today(), result, feedback, {f"experience:{feedback}"})
    assert memory.persistent.dissonance == pytest.approx(0.55)
    assert not await memory.save_dream(date.today(), result, feedback, {f"experience:{feedback}"})
    assert memory.persistent.dissonance == pytest.approx(0.55)


async def test_empty_context_summary_warns_without_changing_saved_history(fake_llm_factory):
    memory = MemoryManager()
    for index in range(12):
        await memory.add_context("user" if index % 2 == 0 else "muika", "An older conversation " * 200)
    request = ModelRequest("Keep my current question", history=list(memory.recent_turns))
    compactor = ContextCompactor(fake_llm_factory(response=ModelCompletions(text="")))
    with pytest.warns(ContextOverflowWarning, match="summary was empty"):
        prepared, _ = await memory.prepare_context(
            SimpleNamespace(
                config=ModelConfig(provider="_echo", context_window=8192, max_tokens=1024), compactor=compactor
            ),
            request,
            [],
        )
    restored = MemoryManager()
    await restored.load()
    assert prepared is request
    assert not restored.snapshot.working_summary and restored.snapshot.summary_through == 0
    assert not restored.recent_turns


def _visible_lasting_tokens(text: str) -> int:
    ascii_count = sum(ord(char) < 128 for char in text)
    return math.ceil(ascii_count / 4 + (len(text) - ascii_count) / 1.3)


def test_lasting_prompt_lists_open_wishes_newest_first():
    moment = datetime(2026, 10, 2, 8)
    state = PersistentState(
        mood="calm",
        reason="He came back.",
        intentions=[
            Intention(id="mango", description="想读诗。", updated_at=moment),
            Intention(id="apple", description="想写信。", updated_at=moment),
            Intention(id="later", description="想散步。", updated_at=moment + timedelta(hours=1)),
            Intention(
                id="waiting",
                description="等他的回信。",
                status="awaiting_feedback",
                updated_at=moment - timedelta(hours=1),
            ),
            Intention(id="done", description="已经放下的事。", status="resolved", updated_at=moment),
        ],
    )
    rendered = state.describe()
    assert rendered.index("later") < rendered.index("apple") < rendered.index("mango") < rendered.index("waiting")
    assert "awaiting_feedback" in rendered
    assert "done" not in rendered and "已经放下的事" not in rendered


def test_lasting_prompt_keeps_a_recognizable_prefix_when_the_wish_does_not_fit():
    moment = datetime(2026, 10, 1, 12)
    newest = "把日报日期改对。" + "等" * 580
    middle = "想知道通道改动是不是为了让话到达他。" + "问" * 30
    oldest = "没有清单的相处"
    closed = "这条已经放弃。"
    intentions = [
        Intention(id="quiet_time", description=oldest, updated_at=moment),
        Intention(
            id="ask_channel",
            description=middle,
            status="acting",
            task_id="task9",
            updated_at=moment + timedelta(hours=1),
        ),
        Intention(id="fix_date", description=newest, updated_at=moment + timedelta(hours=2)),
        Intention(id="old_note", description=closed, status="abandoned", updated_at=moment + timedelta(hours=3)),
    ]
    rendered = PersistentState(mood="calm", reason="He came back.", intentions=intentions).describe()
    assert "Lasting feeling: calm" in rendered
    assert "Why: He came back." in rendered
    assert "Unresolved tension: 0.00" in rendered
    assert rendered.index("fix_date") < rendered.index("ask_channel")
    assert newest in rendered
    assert middle[:24] in rendered and middle[24:] not in rendered
    assert "Intention ask_channel (acting):" in rendered
    assert "[action task: task9]" in rendered
    assert "quiet_time" not in rendered and oldest not in rendered
    assert "old_note" not in rendered and closed not in rendered
    assert _visible_lasting_tokens(rendered) <= PROMPT_LASTING_TOKENS
    assert [item.description for item in intentions] == [oldest, middle, newest, closed]


def test_lasting_prompt_keeps_the_feeling_when_it_already_fills_the_budget():
    reason = "因" * 800
    state = PersistentState(
        mood="calm",
        reason=reason,
        intentions=[Intention(id="fix_date", description="把日报日期改对。", updated_at=datetime(2026, 10, 1))],
    )
    rendered = state.describe()
    assert reason in rendered
    assert "fix_date" not in rendered and "把日报日期改对" not in rendered
    assert state.intentions[0].description == "把日报日期改对。"


def test_visible_working_summary_within_budget_returns_unchanged():
    short_summary = "我们安静地聊了彼此的心情，沐沐提到了最近的作息。"
    assert _visible_working_summary(short_summary) == short_summary


def test_visible_working_summary_keeps_newest_paragraphs_in_chronological_order():
    p1 = "最早聊的话题：" + "前" * 300
    p2 = "中间聊的话题：" + "中" * 200
    p3 = "最近聊的话题：" + "后" * 150
    full_text = f"{p1}\n\n{p2}\n\n{p3}"
    visible = _visible_working_summary(full_text)
    assert len(visible) <= PROMPT_WORKING_SUMMARY_CHARS
    assert p1 not in visible
    assert "中间聊的话题" in visible
    assert "最近聊的话题" in visible
    # 验证按时间先后顺序排列（最新段落仍在最末）
    assert visible.index("中间聊的话题") < visible.index("最近聊的话题")


def test_visible_working_summary_single_oversized_paragraph_tail_fallback():
    oversized = "开头历史" + "字" * 600 + "关键末尾"
    visible = _visible_working_summary(oversized)
    assert len(visible) <= PROMPT_WORKING_SUMMARY_CHARS
    assert "关键末尾" in visible
    assert "开头历史" not in visible


def test_visible_working_summary_sentence_aligned_tail_has_no_half_sentence():
    text = "甲" * 400 + "。" + "乙" * 400 + "。"
    visible = _visible_working_summary(text)
    assert len(visible) <= PROMPT_WORKING_SUMMARY_CHARS
    assert visible.startswith("乙")
    assert "甲" not in visible


def test_visible_working_summary_keeps_aligned_sentence_start():
    text = "甲" * 500 + "。" + "乙" * 500
    visible = _visible_working_summary(text)
    assert len(visible) <= PROMPT_WORKING_SUMMARY_CHARS
    assert visible.startswith("乙")


async def test_prepare_context_clips_working_summary_in_system_without_mutating_snapshot():
    memory = MemoryManager()
    await memory.load()
    long_summary = "早期事件记录。\n\n" + "长段叙事。" * 200 + "\n\n最近我们聊了雨天和诗。"
    memory.snapshot.working_summary = long_summary
    request = ModelRequest("当前用户的问题", history=[])
    model = SimpleNamespace(
        config=ModelConfig(
            provider="openai", model_name="test", api_key="test", context_window=1000000, max_tokens=1000
        )
    )

    prepared, _ = await memory.prepare_context(model, request, [])
    system_text = prepared.system or ""
    assert "[Working context summary]\n" in system_text
    summary_block = system_text.split("[Working context summary]\n")[1]
    assert len(summary_block) <= PROMPT_WORKING_SUMMARY_CHARS
    assert "最近我们聊了雨天和诗" in summary_block
    # 核心约束：只读投影，绝不把截断结果写回 snapshot
    assert memory.snapshot.working_summary == long_summary


async def test_pruned_working_summary_topics_remain_retrievable_from_experience():
    memory = MemoryManager()
    await memory.load()
    # 模拟一条被工作摘要淘汰但在经历库中留存的历史对话
    exp_id = await memory.add_context("user", "沐沐在很久以前提过的一个极其生僻的冷门话题暗号xyz789")
    # 构造超长旧摘要，早期包含该冷门话题，最新段落为其他话题
    old_topic = "早期我们曾聊过冷门话题暗号xyz789。\n\n" + "中间其他无关叙事。" * 60
    recent_topic = "最新我们正在讨论关于今晚写代码的安排。"
    memory.snapshot.working_summary = f"{old_topic}\n\n{recent_topic}"

    model = SimpleNamespace(
        config=ModelConfig(
            provider="openai", model_name="test", api_key="test", context_window=1000000, max_tokens=1000
        )
    )
    prepared, _ = await memory.prepare_context(model, ModelRequest("用户新消息"), [])
    system_text = prepared.system or ""
    # 验证冷门话题确实被 _visible_working_summary 裁出注入视图
    assert "xyz789" not in system_text
    assert "今晚写代码" in system_text
    # 验证连续性兜底：尽管工作摘要被裁，该经历依然在数据库中完整保留，可随时通过 read_source 调取
    original = await memory.read_source(f"experience:{exp_id}")
    assert "xyz789" in original


async def test_summarize_dialogue_force_records_unreplied_proactive_hook(fake_llm_factory):
    memory = MemoryManager()
    await memory.load()
    proactive_text = "半梦半醒想起吉萨大金字塔与登月的时间错位，你醒了可以看看。"
    await memory.add_context("muika", proactive_text)
    # 模拟沐沐尚未回复
    captured_requests = []

    def fake_ask(req):
        captured_requests.append(req)
        return ModelCompletions(text="Muika 主动分享了金字塔与登月的时间错位，沐沐暂未回复。")

    model = fake_llm_factory(side_effect=fake_ask)
    success = await memory.summarize_dialogue(model, force=True)
    assert success
    # 验证行为：未回复的主动消息被完整送入待摘要材料的 prompt 中
    assert proactive_text in captured_requests[0].prompt
    assert "金字塔与登月" in memory.snapshot.latest_dialogue_summary
    assert len(memory.snapshot.latest_dialogue_summary) <= PROMPT_WORKING_SUMMARY_CHARS


async def test_prepare_context_compaction_clips_system_while_preserving_full_working_summary():
    memory = MemoryManager()
    await memory.load()
    for i in range(10):
        await memory.add_context("user" if i % 2 == 0 else "muika", f"历史对话回合内容 {i} " * 30)

    full_compactor_output = (
        "压缩产出的新工作摘要开头。\n\n" + "很长很长的中间工作总结。" * 50 + "\n\n压缩产出的最新工作摘要末尾。"
    )
    compactor = SimpleNamespace(summarize=AsyncMock(return_value=full_compactor_output))

    config = ModelConfig(provider="openai", model_name="test", api_key="test", context_window=8192, max_tokens=512)
    model = SimpleNamespace(config=config, compactor=compactor)

    request = ModelRequest("当前新问题", history=list(memory.recent_turns))
    compressed, _ = await memory.prepare_context(model, request, [], force=True)

    system_text = compressed.system or ""
    assert "[Working context summary]\n" in system_text
    summary_block = system_text.split("[Working context summary]\n")[1]
    # 验证注入给模型的 system 提示词严格遵守 500 字符限制
    assert len(summary_block) <= PROMPT_WORKING_SUMMARY_CHARS
    assert "最新工作摘要末尾" in summary_block
    # 核心约束：snapshot.working_summary 保存的是 compactor 的完整生成文本，绝不被截断回写
    assert memory.snapshot.working_summary == full_compactor_output
    assert len(memory.snapshot.working_summary) > PROMPT_WORKING_SUMMARY_CHARS
