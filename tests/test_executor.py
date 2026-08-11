"""``Executor._split_message`` 切分算法与 ``send_message`` 发送行为测试。"""

import asyncio
from unittest.mock import AsyncMock

from muika.core.executor import Executor

# ---------------------------------------------------------------------------
# _split_message —— 纯静态方法
# ---------------------------------------------------------------------------


def test_split_empty_returns_single_empty():
    assert Executor._split_message("") == [""]


def test_split_short_text_unchanged():
    assert Executor._split_message("hello") == ["hello"]


def test_split_paragraphs_stay_separate():
    assert Executor._split_message("a\n\nb") == ["a", "b"]


def test_split_merges_sentences_greedily():
    result = Executor._split_message("A。B。C。D。E。F。G。", 10)
    assert result == ["A。B。C。D。E。", "F。G。"]


def test_split_oversized_segment_hard_split():
    content = "x" * 600
    result = Executor._split_message(content, 250)
    assert result == ["x" * 250, "x" * 250, "x" * 100]


def test_split_custom_max_length():
    result = Executor._split_message("A。B。", 3)
    assert result == ["A。", "B。"]


# ---------------------------------------------------------------------------
# send_message —— 通过假 send_func 观察分段与资源携带
# ---------------------------------------------------------------------------


async def test_send_message_strips_and_delivers(monkeypatch):
    sent = []

    async def send_func(msg, resources, target):
        sent.append((msg, resources, target))

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.asyncio.sleep", AsyncMock())

    await executor.send_message("  hello  ")
    assert len(sent) == 1
    assert sent[0] == ("hello", None, None)


async def test_send_message_resources_only_last_segment(monkeypatch):
    sent = []

    async def send_func(msg, resources, target):
        sent.append(resources)

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.asyncio.sleep", AsyncMock())

    await executor.send_message("a\n\nb", resources=["r1"])
    assert sent == [None, ["r1"]]


async def test_send_message_empty_uses_single_segment(monkeypatch):
    sent = []

    async def send_func(msg, resources, target):
        sent.append((msg, resources, target))

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.asyncio.sleep", AsyncMock())

    await executor.send_message("")
    assert sent == [("", None, None)]
