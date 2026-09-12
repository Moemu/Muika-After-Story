"""模型步骤、工具批次和原始协议的行为检查。"""

import json
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from muika.llm import ModelCompletions, ModelConfig, ModelRequest
from muika.llm import _wrapper as usage_wrapper
from muika.llm._execution import collect_step, run_conversation
from muika.llm._schema import (
    MediaReference,
    ModelMessage,
    ModelStreamCompletions,
    ToolResult,
    Usage,
)
from muika.llm.providers.openai import Openai


def _provider():
    return Openai(ModelConfig(provider="openai", model_name="test", api_key="test"))


def _response(message, finish_reason="stop"):
    return ChatCompletion(
        id="test",
        created=1,
        model="test",
        object="chat.completion",
        choices=[{"index": 0, "finish_reason": finish_reason, "message": message}],
        usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    )


@pytest.mark.parametrize("mode", ["step", "complete", "stream", "closed_stream"])
async def test_usage_records_cumulative_tokens_and_original_owner(monkeypatch, db_session, session_ctx_factory, mode):
    provider = _provider()
    monkeypatch.setattr(usage_wrapper, "get_session", lambda: session_ctx_factory(db_session))
    monkeypatch.setattr(usage_wrapper, "_get_caller_plugin_name", lambda: "original")
    monkeypatch.setattr(usage_wrapper, "get_name_from_config", lambda config: "test-model")

    async def chunks(request, messages, *, stream):
        for output_tokens in (1, 2):
            yield ModelStreamCompletions(chunk="text", usage=Usage(3, output_tokens, 2))

    monkeypatch.setattr(provider, "request_step", chunks)
    request = ModelRequest("hello")
    if mode == "step":
        await provider.step(request, [])
    elif mode == "complete":
        await provider.ask(request)
    else:
        response = await provider.ask(request, stream=True)
        monkeypatch.setattr(usage_wrapper, "_get_caller_plugin_name", lambda: "consumer")
        monkeypatch.setattr(usage_wrapper, "get_name_from_config", lambda config: "changed-model")
        if mode == "closed_stream":
            await anext(response)
            await response.aclose()
        else:
            async for _ in response:
                pass

    records = await usage_wrapper.UsageORM.get_usage_records(db_session)
    assert len(records) == 1
    record = records[0]
    assert (record.plugin, record.model) == ("original", "test-model")
    assert (record.input_tokens, record.output_tokens, record.cached_tokens) == (
        3,
        1 if mode == "closed_stream" else 2,
        2,
    )


@pytest.mark.parametrize("stream", [False, True])
async def test_structured_content_preserves_json_and_private_reasoning(monkeypatch, stream):
    provider = _provider()
    provider.config.stream = stream
    body = json.dumps({"value": "literal <think>text</think> and 'quotes'"})

    async def chunks():
        for delta, reason in [({"reasoning_content": "private"}, None), ({"content": body}, "stop")]:
            yield ChatCompletionChunk(
                id="test",
                created=1,
                model="test",
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta, "finish_reason": reason}],
            )

    async def create(**kwargs):
        assert kwargs["response_format"] == {"type": "json_object"}
        if kwargs["stream"]:
            return chunks()
        return _response({"role": "assistant", "content": body, "reasoning_content": "private"})

    monkeypatch.setattr(provider.client.chat.completions, "create", create)
    result = await provider._collect_stream(
        run_conversation(provider, ModelRequest("classify", format="json"), stream=False)
    )
    assert result.require_content() == body
    assert result.text == f"<think>private</think>{body}"
    assert result.message.reasoning == "private"


@pytest.mark.parametrize(
    "completion",
    [ModelCompletions(text="{}", succeed=False), ModelCompletions(text="{}", stop_reason="length")],
)
def test_structured_content_rejects_failed_or_incomplete_response(completion):
    with pytest.raises(ValueError):
        completion.require_content()


async def test_tool_image_is_in_next_provider_request(monkeypatch, tmp_path):
    provider = _provider()
    provider.config.multimodal = True
    picture = tmp_path / "render.png"
    picture.write_bytes(b"image-evidence")
    batch = _response(
        {
            "role": "assistant",
            "tool_calls": [{"id": "image", "type": "function", "function": {"name": "view_image", "arguments": "{}"}}],
        },
        "tool_calls",
    )
    requests = []

    async def create(**kwargs):
        requests.append(kwargs["messages"])
        return batch if len(requests) == 1 else _response({"role": "assistant", "content": "inspected"})

    async def handler(name, arguments):
        return ToolResult(text="render", resources=[MediaReference(type="image", path=str(picture))])

    monkeypatch.setattr(provider.client.chat.completions, "create", create)
    monkeypatch.setattr("muika.llm.utils.tools.function_call_handler", handler)
    await provider._collect_stream(run_conversation(provider, ModelRequest("inspect"), stream=False))
    observation = requests[1][-1]
    assert observation["role"] == "user"
    assert observation["content"][1]["image_url"]["url"].endswith("aW1hZ2UtZXZpZGVuY2U=")


async def test_tool_batch_preserves_quotes_and_finishes_before_next_request(monkeypatch):
    provider = _provider()
    requests = []
    calls = []
    batch = _response(
        {
            "role": "assistant",
            "content": "Reading",
            "reasoning_content": "private",
            "tool_calls": [
                {
                    "id": "one",
                    "type": "function",
                    "function": {"name": "execute_python", "arguments": json.dumps({"code": "print('hello')"})},
                },
                {"id": "two", "type": "function", "function": {"name": "read_file", "arguments": "{bad JSON"}},
                {
                    "id": "three",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"result.txt"}'},
                },
            ],
        },
        "tool_calls",
    )

    async def create(**kwargs):
        requests.append(kwargs["messages"])
        return batch if len(requests) == 1 else _response({"role": "assistant", "content": "done"})

    async def handler(name, arguments):
        calls.append((name, arguments))
        return "ok"

    monkeypatch.setattr(provider.client.chat.completions, "create", create)
    monkeypatch.setattr("muika.llm.utils.tools.function_call_handler", handler)
    result = await provider._collect_stream(run_conversation(provider, ModelRequest("work"), stream=False))
    assert result.text == "done"
    assert calls == [("execute_python", {"code": "print('hello')"}), ("read_file", {"path": "result.txt"})]
    tool_results = [m for m in requests[1] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["one", "two", "three"]
    assert "Invalid arguments" in tool_results[1]["content"]
    assert requests[1][1]["reasoning_content"] == "private"
    assert result.usage.input_tokens == 6


async def test_interleaved_stream_accumulates_all_calls(monkeypatch):
    provider = _provider()
    provider.config.stream = True
    fragments = [
        {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read_file", "arguments": '{"path":'}}]},
        {"tool_calls": [{"index": 1, "id": "b", "function": {"name": "read_file", "arguments": '{"path":"b"}'}}]},
        {"tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]},
    ]

    async def stream():
        for delta in fragments:
            yield ChatCompletionChunk(
                id="stream",
                created=1,
                model="test",
                object="chat.completion.chunk",
                choices=[{"index": 0, "delta": delta}],
            )
        yield ChatCompletionChunk(
            id="stream",
            created=1,
            model="test",
            object="chat.completion.chunk",
            choices=[{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
        )

    monkeypatch.setattr(provider.client.chat.completions, "create", AsyncMock(return_value=stream()))
    result = await collect_step(provider, ModelRequest("read"), [])
    assert result.message is not None
    assert [(c.id, json.loads(c.arguments)) for c in result.message.tool_calls] == [
        ("a", {"path": "a"}),
        ("b", {"path": "b"}),
    ]


async def test_tool_error_does_not_repeat_successful_calls(monkeypatch):
    provider = _provider()
    responses = [
        _response(
            {
                "role": "assistant",
                "tool_calls": [{"id": "a", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}],
            },
            "tool_calls",
        ),
        _response({"role": "assistant", "content": "blocked"}),
    ]
    monkeypatch.setattr(provider.client.chat.completions, "create", AsyncMock(side_effect=responses))
    handler = AsyncMock(return_value=ToolResult(text="write failed", is_error=True))
    monkeypatch.setattr("muika.llm._execution.execute_call", handler)
    result = await provider._collect_stream(run_conversation(provider, ModelRequest("work"), stream=False))
    assert result.text == "blocked"
    handler.assert_awaited_once()


def test_gemini_keeps_signature_and_multiple_results():
    pytest.importorskip("google.genai")
    from google.genai.types import Content, FunctionCall, Part

    from muika.llm.providers.gemini import Gemini

    provider = Gemini(ModelConfig(provider="gemini", model_name="test", api_key="test"))
    native = Content(
        role="model",
        parts=[
            Part(
                function_call=FunctionCall(id="a", name="read_file", args={"path": "a"}), thought_signature=b"\xff\x00"
            ),
            Part(function_call=FunctionCall(id="b", name="read_file", args={"path": "b"})),
        ],
    )
    message = ModelMessage(role="assistant", provider_data={"gemini_content": native.model_dump(mode="json")})
    restored = ModelMessage.model_validate_json(message.model_dump_json())
    messages = provider._conversation_messages(
        ModelRequest("read"),
        [
            restored,
            ModelMessage(role="tool", tool_call_id="a", name="read_file", content="one"),
            ModelMessage(role="tool", tool_call_id="b", name="read_file", content="two"),
        ],
    )
    assert messages[1].parts[0].thought_signature == b"\xff\x00"
    assert [p.function_response.id for p in messages[2].parts] == ["a", "b"]
