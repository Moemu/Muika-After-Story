"""web_search 工具的后端响应解析边界。"""

import pytest


@pytest.mark.parametrize("data", [None, [], 42, {}, {"results": None}, {"results": "nope"}])
def test_malformed_payload_raises(data, web_search_plugin):
    with pytest.raises(ValueError):
        web_search_plugin._parse_tavily_response(data)
    with pytest.raises(ValueError):
        web_search_plugin._parse_perplexity_response(data)
    with pytest.raises(ValueError):
        web_search_plugin._parse_deepseek_response(data)


def test_items_without_url_are_skipped(web_search_plugin):
    data = {
        "results": [
            {"title": "no url here"},
            {"url": "https://example.com/c", "title": None, "snippet": None, "date": None},
        ]
    }
    results = web_search_plugin._parse_perplexity_response(data)
    assert [item.url for item in results] == ["https://example.com/c"]
    assert results[0].title == "https://example.com/c"
    assert results[0].snippet == ""
    assert results[0].date is None


def test_snippet_is_truncated(web_search_plugin):
    data = {"results": [{"url": "https://example.com/d", "content": "x" * 2000}]}
    (result,) = web_search_plugin._parse_tavily_response(data)
    assert len(result.snippet) == 800


def test_empty_results_return_empty_list(web_search_plugin):
    assert web_search_plugin._parse_tavily_response({"results": []}) == []
    assert web_search_plugin._parse_perplexity_response({"results": []}) == []
    assert web_search_plugin._parse_deepseek_response({"results": []}) == []
