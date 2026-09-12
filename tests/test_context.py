"""验证模型预算、完整工具交互压缩和超长重试。"""

from unittest.mock import AsyncMock

import pytest

from muika.core.memory import MemoryManager
from muika.llm import ModelCompletions, ModelConfig, ModelRequest
from muika.llm._execution import collect_step, run_conversation
from muika.llm._retry import LLMRequestError
from muika.llm._schema import (
    ModelMessage,
    ModelStreamCompletions,
    ToolCall,
    ToolResult,
    Usage,
)
from muika.llm.context import (
    ContextCompactor,
    ContextOverflowWarning,
    estimate_tokens,
    input_budget,
    request_tokens,
    split_text,
)
from muika.llm.providers.openai import Openai
from muika.utils.logger import logger


def test_output_and_separate_thinking_are_reserved():
    config = ModelConfig(
        provider="dashscope", context_window=32000, max_tokens=4000, enable_thinking=True, thinking_budget=8000
    )
    assert input_budget(config) == 18400
    config.enable_thinking = False
    assert input_budget(config) == 26400
    with pytest.warns(ContextOverflowWarning):
        assert input_budget(ModelConfig(provider="_echo", context_window=4096, max_tokens=4096)) == 0


def test_split_preserves_original_without_exceeding_budget():
    from muika.llm.context import estimate_tokens

    source = "一段诗。" * 500 + "\n" + "word " * 1000
    chunks = split_text(source, 700)
    assert "".join(chunks) == source
    assert all(estimate_tokens(chunk) <= 700 for chunk in chunks)


def test_fitting_multiline_source_does_not_create_a_tiny_extra_summary_request():
    source = "First observation.\n" + "A verified action result. " * 10 + "\nLast line."
    assert split_text(source, estimate_tokens(source)) == [source]


async def test_tool_group_compaction_keeps_pairing_signatures_and_original_output(
    fake_llm_factory, redirect_get_session
):
    summary_model = fake_llm_factory(
        response=ModelCompletions(text="Verified outcome; the next step is still pending.")
    )
    compactor = ContextCompactor(summary_model)
    config = ModelConfig(provider="_echo", context_window=8192, max_tokens=1024)
    first = ModelMessage(role="assistant", tool_calls=[ToolCall(id="a", name="read", arguments="{}")])
    last = ModelMessage(
        role="assistant",
        content="Continue",
        reasoning="private current reasoning",
        provider_state={"signature": "required"},
        tool_calls=[ToolCall(id="b", name="write", arguments="{}")],
    )
    original = [
        first,
        ModelMessage(role="tool", tool_call_id="a", content="old evidence " * 3000),
        last,
        ModelMessage(role="tool", tool_call_id="b", content="new output " * 3000 + "exact tail"),
    ]
    request = ModelRequest(prompt="Keep the current objective", tools=[{"function": {"name": "write"}}])
    compacted, through, summary = await compactor.compact_messages(request, original, config)
    assert through == 2 and summary
    assert compacted[1] == last
    assert compacted[2].tool_call_id == "b"
    assert request_tokens(request, compacted) <= input_budget(config)
    assert original[-1].content.endswith("exact tail")
    ref = compacted[-1].content.split("Full tool result: ")[1].split("]")[0]
    memory = MemoryManager()
    full = await memory.read_source(ref, offset=len(original[-1].content) - 20)
    assert "exact tail" in full
    assert "private current reasoning" not in summary


async def test_context_length_retry_does_not_reexecute_tools(monkeypatch, fake_llm_factory):
    model = Openai(
        ModelConfig(provider="openai", model_name="test", api_key="test", context_window=8192, max_tokens=1024)
    )
    summary = fake_llm_factory(response=ModelCompletions(text="Completed once; inspect the retained result."))
    model.compactor = ContextCompactor(summary)
    call = ToolCall(id="write1", name="write_once", arguments="{}")
    requests = []

    async def step(request, messages, *, stream):
        requests.append([item.model_copy(deep=True) for item in messages])
        if len(requests) == 1:
            yield ModelStreamCompletions(
                message=ModelMessage(role="assistant", tool_calls=[call]), stop_reason="tool_calls"
            )
        elif len(requests) == 2:
            raise LLMRequestError("maximum context length exceeded", "context_length", 400)
        else:
            yield ModelStreamCompletions(chunk="Verified.", message=ModelMessage(role="assistant", content="Verified."))

    action = AsyncMock(return_value=ToolResult(text="saved " * 1200))
    monkeypatch.setattr(model, "request_step", step)
    monkeypatch.setattr("muika.llm._execution.execute_call", action)
    chunks = [chunk async for chunk in run_conversation(model, ModelRequest("Perform once"), stream=False)]
    assert chunks[-1].chunk == "Verified."
    assert action.await_count == 1 and len(requests) == 3
    assert requests[-1][0].tool_calls[0].id == "write1"
    assert requests[-1][1].tool_call_id == "write1"


async def test_oversized_current_prompt_warns_and_reaches_provider_unchanged(monkeypatch):
    model = Openai(
        ModelConfig(provider="openai", model_name="test", api_key="test", context_window=4096, max_tokens=1024)
    )
    sent = []

    async def step(request, messages, *, stream):
        sent.append(request)
        yield ModelStreamCompletions(chunk="unexpected")

    monkeypatch.setattr(model, "request_step", step)
    request = ModelRequest("当前请求" * 5000)
    with pytest.warns(ContextOverflowWarning):
        result = await collect_step(model, request, [])
    assert result.succeed and sent == [request]
    assert sent[0].prompt == "当前请求" * 5000


async def test_model_switch_recompresses_saved_summary(redirect_get_session):
    memory = MemoryManager()
    memory.snapshot.working_summary = "old context " * 3000
    compactor = AsyncMock(spec=ContextCompactor)
    compactor.summarize.return_value = "A shorter source-backed working summary."
    request = await memory.prepare_context(
        ModelRequest("new question"), ModelConfig(provider="_echo", context_window=4096, max_tokens=1024), compactor
    )
    assert "new question" == request.prompt
    assert memory.snapshot.working_summary == "A shorter source-backed working summary."


@pytest.mark.parametrize("stream", [False, True])
async def test_actual_service_length_rejection_remains_a_failed_response(monkeypatch, stream):
    model = Openai(ModelConfig(provider="openai", model_name="test", api_key="test"))
    calls = []

    async def step(request, messages, *, stream):
        calls.append(request)
        raise LLMRequestError("service context limit", "context_length", 400)
        yield

    monkeypatch.setattr(model, "request_step", step)
    chunks = [chunk async for chunk in run_conversation(model, ModelRequest("current input"), stream=stream)]
    assert len(calls) == 1
    assert not chunks[-1].succeed and "service context limit" in chunks[-1].chunk


async def test_failed_summary_keeps_complete_tool_protocol(fake_llm_factory):
    compactor = ContextCompactor(fake_llm_factory(response=ModelCompletions(text="")))
    messages = [
        ModelMessage(role="assistant", tool_calls=[ToolCall(id="old", name="read", arguments="{}")]),
        ModelMessage(role="tool", tool_call_id="old", content="evidence " * 3000),
        ModelMessage(role="assistant", content="Continue from that result."),
    ]
    with pytest.warns(ContextOverflowWarning, match="summary was empty"):
        result, through, summary = await compactor.compact_messages(
            ModelRequest("current input"), messages, ModelConfig(provider="_echo", context_window=8192, max_tokens=1024)
        )
    assert result == messages and through == 0 and summary == ""


async def test_summary_above_target_is_used_once_when_it_fits_available_budget(fake_llm_factory):
    summary = "Source-backed outcome. " * 600
    model = fake_llm_factory(response=ModelCompletions(text=summary))
    assert 4096 < estimate_tokens(summary) < 8000
    result = await ContextCompactor(model).summarize("Long evidence. " * 6000, 4096, available_tokens=8000)
    assert result == summary.strip()
    assert model.call_count == 1


async def test_summary_never_uses_a_larger_than_available_result(fake_llm_factory):
    model = fake_llm_factory(response=ModelCompletions(text="Unchanged long summary. " * 700))
    with pytest.warns(ContextOverflowWarning, match="did not fit"):
        result = await ContextCompactor(model).summarize("Long evidence. " * 4000, 1000, available_tokens=2000)
    assert result is None


async def test_model_timing_reports_usage_without_prompt_or_reasoning(monkeypatch):
    model = Openai(ModelConfig(provider="openai", model_name="test", api_key="test"))
    logs = []
    console = []
    debug_sink = logger.add(logs.append, level="DEBUG")
    console_sink = logger.add(console.append, level="INFO")

    async def step(request, messages, *, stream):
        yield ModelStreamCompletions(
            chunk="Visible response",
            message=ModelMessage(role="assistant", reasoning="private reflection"),
            usage=Usage(input_tokens=123, output_tokens=45, cached_tokens=67),
        )

    try:
        monkeypatch.setattr(model, "request_step", step)
        assert (await collect_step(model, ModelRequest("private input"), [])).succeed
    finally:
        logger.remove(debug_sink)
        logger.remove(console_sink)
    assert not console
    assert any("[Context] prepared" in log and "input_after=" in log for log in logs)
    assert any("[Model] start" in log for log in logs)
    assert any(
        "first_chunk_seconds=" in log and "input_tokens=123 output_tokens=45 cached_tokens=67" in log for log in logs
    )
    assert all("private input" not in log and "private reflection" not in log for log in logs)
