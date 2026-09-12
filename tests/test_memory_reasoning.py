"""验证检索降级和有预算的日记生成。"""

from datetime import date, datetime

from muika.core.memory import MemoryManager
from muika.core.memory_models import DreamResult
from muika.core.memory_reasoning import MemoryReasoner
from muika.llm import ModelCompletions, ModelConfig
from muika.llm.context import input_budget, request_tokens


async def test_query_expansion_finds_synonyms_and_dates(fake_llm_factory, redirect_get_session):
    memory = MemoryManager()
    ref = await memory.add_context("user", "I love oolong.", timestamp=datetime(2026, 9, 1, 12))

    def respond(request):
        assert request.json_schema is None
        if "MemoryQuery" in request.system:
            return ModelCompletions(text='{"terms":["oolong","tea"],"start":"2026-09-01","end":"2026-09-01"}')
        assert "RecallSelection" in request.system
        return ModelCompletions(text=f'{{"refs":["experience:{ref}"]}}')

    model = fake_llm_factory(side_effect=respond)
    result = await MemoryReasoner(model, model).recall("What drink did I like on September 1?", memory)
    assert [hit.ref for hit in result.hits] == [f"experience:{ref}"]
    assert not result.degraded


async def test_failed_semantic_recall_preserves_keyword_results(fake_llm_factory, redirect_get_session):
    memory = MemoryManager()
    await memory.add_context("user", "oolong tea", timestamp=datetime(2026, 9, 1))
    model = fake_llm_factory(error=RuntimeError("offline"))
    result = await MemoryReasoner(model, model).recall("oolong 2026-09-01", memory)
    assert result.degraded and result.hits
    assert "unavailable" in result.describe() and "oolong tea" in result.describe()


async def test_long_day_is_chunked_then_committed_as_one_diary(fake_llm_factory, redirect_get_session):
    memory = MemoryManager()
    day = date(2026, 9, 1)
    ref = await memory.add_context("muika", "I read a poem. " * 6000, timestamp=datetime(2026, 9, 1, 12))

    def respond(request):
        assert request_tokens(request) < input_budget(model.config)
        if request.format == "json":
            assert request.json_schema is None and "DreamResult" in request.system
            return ModelCompletions(
                text=DreamResult(diary="I read a poem and wondered about its rhythm.").model_dump_json()
            )
        return ModelCompletions(text=f"[experience:{ref}] I read a poem and wondered about its rhythm.")

    model = fake_llm_factory(side_effect=respond)
    model.config = ModelConfig(provider="_echo", context_window=16384, max_tokens=2048)
    assert await MemoryReasoner(model, model).dream(day, memory)
    assert model.call_count > 2
    diaries = await memory.recent_diaries(day)
    assert len(diaries) == 1 and diaries[0].source == "dream:2026-09-01"
    assert len((await memory.day_material(day))[0].content) > 60000


async def test_unknown_selected_reference_keeps_candidate_evidence(fake_llm_factory, redirect_get_session):
    memory = MemoryManager()
    ref = await memory.add_context("user", "I love oolong.")
    replies = iter(['{"terms":["oolong"]}', '{"refs":["I love oolong."]}'])
    model = fake_llm_factory(side_effect=lambda request: ModelCompletions(text=next(replies)))
    result = await MemoryReasoner(model, model).recall("What tea do I like?", memory)
    assert result.degraded and "unknown source" in result.error
    assert [hit.ref for hit in result.hits] == [f"experience:{ref}"]


async def test_recall_keeps_its_model_when_configuration_changes_between_calls(fake_llm_factory, redirect_get_session):
    memory = MemoryManager()
    ref = await memory.add_context("user", "I love oolong.")
    replacement = fake_llm_factory(error=AssertionError("Only future recalls use the replacement"))

    def respond(request):
        reasoner.model = replacement
        return ModelCompletions(
            text='{"terms":["oolong"]}' if "MemoryQuery" in request.system else f'{{"refs":["experience:{ref}"]}}'
        )

    original = fake_llm_factory(side_effect=respond)
    reasoner = MemoryReasoner(original, original)
    result = await reasoner.recall("What tea do I like?", memory)
    assert not result.degraded and [hit.ref for hit in result.hits] == [f"experience:{ref}"]
    assert original.call_count == 2 and replacement.call_count == 0
