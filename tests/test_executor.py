"""验证消息分段投递、资源携带和传输回执。"""

import asyncio

import pytest

from muika.core.executor import Executor, SendReceipt
from muika.models import Resource


@pytest.mark.parametrize(
    ("message", "segments"),
    [
        pytest.param("  hello  ", ["hello"], id="strip"),
        pytest.param(("a" * 89 + "。") * 3, [("a" * 89 + "。") * 2, "a" * 89 + "。"], id="sentences"),
        pytest.param("x" * 600, ["x" * 250, "x" * 250, "x" * 100], id="hard-split"),
        pytest.param("", [""], id="empty"),
    ],
)
async def test_send_message_delivers_segments(monkeypatch, message, segments):
    sent = []

    async def send_func(msg, resources, target):
        sent.append((msg, resources, target))

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.DELAYED_SECOND_PER_PARAGRAPH", 0)

    await executor.send_message(message)
    assert sent == [(segment, None, None) for segment in segments]


async def test_send_message_resources_only_last_segment(monkeypatch):
    sent = []
    resources = [Resource(type="image", path="render.png", mimetype="image/png")]

    async def send_func(msg, resources, target):
        sent.append((msg, resources, target))

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.DELAYED_SECOND_PER_PARAGRAPH", 0)

    await executor.send_message("a\n\nb", resources=resources, target="qq")
    assert sent == [("a", None, "qq"), ("b", resources, "qq")]


@pytest.mark.parametrize(
    ("receipts", "expected"),
    [
        ([SendReceipt.WRITTEN], SendReceipt.WRITTEN),
        ([SendReceipt.WRITTEN, SendReceipt.WRITTEN], SendReceipt.WRITTEN),
        ([SendReceipt.QUEUED], SendReceipt.QUEUED),
        ([SendReceipt.WRITTEN, SendReceipt.QUEUED], SendReceipt.QUEUED),
        ([SendReceipt.FAILED, SendReceipt.QUEUED], SendReceipt.FAILED),
        ([SendReceipt.WRITTEN, SendReceipt.FAILED], SendReceipt.FAILED),
        ([None, None], SendReceipt.WRITTEN),
    ],
)
async def test_send_message_aggregates_segment_receipts(monkeypatch, receipts, expected):
    """任一段 FAILED 即 FAILED，否则任一段 QUEUED 即 QUEUED，缺省回执按 WRITTEN。"""
    deliveries = iter(receipts)

    async def send_func(msg, resources, target):
        return next(deliveries)

    executor = Executor(asyncio.Queue(), send_func)
    monkeypatch.setattr("muika.core.executor.DELAYED_SECOND_PER_PARAGRAPH", 0)

    message = "\n\n".join(f"segment {index}" for index in range(len(receipts)))
    assert await executor.send_message(message) is expected
