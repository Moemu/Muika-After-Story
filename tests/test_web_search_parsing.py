"""web_search 工具的后端响应解析边界。"""

import pytest

from muika.core.actions.tools._search import (
    SearchResult,
    _parse_perplexity_response,
    _parse_tavily_response,
)


def test_tavily_response_maps_normalized_fields():
    data = {
        "results": [
            {
                "title": "Example",
                "url": "https://example.com/a",
                "content": "  hello   world \n next  ",
                "score": 0.9,
                "published_date": "2026-09-01",
            }
        ]
    }
    assert _parse_tavily_response(data) == [
        SearchResult(title="Example", url="https://example.com/a", snippet="hello world next", date="2026-09-01")
    ]


def test_perplexity_response_maps_normalized_fields():
    data = {
        "results": [
            {"title": "Example", "url": "https://example.com/b", "snippet": "snippet text", "date": "2026-09-27"}
        ]
    }
    assert _parse_perplexity_response(data) == [
        SearchResult(title="Example", url="https://example.com/b", snippet="snippet text", date="2026-09-27")
    ]


@pytest.mark.parametrize("data", [None, [], 42, {}, {"results": None}, {"results": "nope"}])
def test_malformed_payload_raises(data):
    with pytest.raises(ValueError):
        _parse_tavily_response(data)
    with pytest.raises(ValueError):
        _parse_perplexity_response(data)


def test_items_without_url_are_skipped():
    data = {
        "results": [
            {"title": "no url here"},
            {"url": "https://example.com/c", "title": None, "snippet": None, "date": None},
        ]
    }
    results = _parse_perplexity_response(data)
    assert [item.url for item in results] == ["https://example.com/c"]
    assert results[0].title == "https://example.com/c"
    assert results[0].snippet == ""
    assert results[0].date is None


def test_snippet_is_truncated():
    data = {"results": [{"url": "https://example.com/d", "content": "x" * 2000}]}
    (result,) = _parse_tavily_response(data)
    assert len(result.snippet) == 800


def test_empty_results_return_empty_list():
    assert _parse_tavily_response({"results": []}) == []
    assert _parse_perplexity_response({"results": []}) == []
