"""验证素材留存、日记事务、事实权重和持续状态。"""

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from muika.core.memory import MemoryCategory, MemoryManager, MemoryQuery, StateUpdate
from muika.core.memory_models import DreamResult, Fact, FactUpdate, Intention
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
    assert len(restored.recent_turns) == 145
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
    assert "coffee" not in memory.get_memory_prompt()
    assert "neighbor_alice.drink" in memory.get_memory_prompt()


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
    with pytest.raises(OSError):
        await memory.prepare_context(request, config, compactor)
    assert list(memory.recent_turns) == before and memory.snapshot.working_summary == ""
    monkeypatch.setattr(memory, "_save_snapshot", save)
    prepared = await memory.prepare_context(request, config, compactor)
    assert prepared.prompt == "The current request" and len(prepared.history) < len(before)
    restored = MemoryManager()
    await restored.load()
    assert restored.snapshot.working_summary == memory.snapshot.working_summary
    assert len(restored.recent_turns) == len(memory.recent_turns)
    assert "Turn 0:" in await restored.read_source("experience:1")


async def test_current_request_is_never_silently_truncated():
    memory = MemoryManager()
    request = ModelRequest(prompt="required current input " * 2000)
    config = ModelConfig(provider="_echo", context_window=4096, max_tokens=1024)
    with pytest.warns(ContextOverflowWarning):
        prepared = await memory.prepare_context(request, config, AsyncMock(spec=ContextCompactor))
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
        prepared = await memory.prepare_context(
            request, ModelConfig(provider="_echo", context_window=8192, max_tokens=1024), compactor
        )
    restored = MemoryManager()
    await restored.load()
    assert prepared is request
    assert not restored.snapshot.working_summary and restored.snapshot.summary_through == 0
    assert list(restored.recent_turns) == list(memory.recent_turns)
