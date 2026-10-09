"""验证有预算的日记生成。"""

from datetime import date, datetime

from muika.core.memory import MemoryManager
from muika.core.memory_models import DreamResult
from muika.core.memory_reasoning import MemoryReasoner
from muika.llm import ModelCompletions, ModelConfig
from muika.llm.context import input_budget, request_tokens


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
    assert await MemoryReasoner(model).dream(day, memory)
    assert model.call_count > 2
    diaries = await memory.recent_diaries(day)
    assert len(diaries) == 1 and diaries[0].source == "dream:2026-09-01"
    assert len((await memory.day_material(day))[0].content) > 60000
