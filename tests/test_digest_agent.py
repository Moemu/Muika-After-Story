"""``DigestAgent`` 的纯逻辑/LLM 部分测试（_normalize_text / _assess_entry_for_muika）。"""

import json

from muika.core.digest_agent import DigestAgent
from muika.llm import ModelCompletions


def test_normalize_text():
    assert DigestAgent._normalize_text("  Hello   World\n ") == "hello world"


async def test_assess_entry_long_content_truncated(fake_llm_factory):
    fake = fake_llm_factory(
        response=ModelCompletions(
            text=json.dumps({"score": 80, "keep": True, "reason": "good", "primary_theme": "tech", "summary": "s"})
        )
    )
    dg = DigestAgent.__new__(DigestAgent)
    dg.model = fake

    result = await dg._assess_entry_for_muika("hn", "A Title", "x" * 5000)
    assert result is not None
    assert result.score == 80
    assert "...(truncated)" in fake.requests[0].prompt


async def test_assess_entry_short_content_none(fake_llm_factory):
    fake = fake_llm_factory(response=ModelCompletions(text="{}"))
    dg = DigestAgent.__new__(DigestAgent)
    dg.model = fake

    # normalize 后长度 < 40 → 直接返回 None，不调用 LLM
    result = await dg._assess_entry_for_muika("hn", "hi", "yo")
    assert result is None
    assert fake.call_count == 0


async def test_assess_entry_llm_failure_none(fake_llm_factory):
    fake = fake_llm_factory(error=RuntimeError("boom"))
    dg = DigestAgent.__new__(DigestAgent)
    dg.model = fake

    result = await dg._assess_entry_for_muika("hn", "A sufficiently long title here", "some content")
    assert result is None
