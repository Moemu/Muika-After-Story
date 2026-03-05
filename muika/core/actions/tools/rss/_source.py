from ._schema import RSSSource

RSS_SOURCES = {
    "hn": RSSSource(
        id="hn",
        name="Hacker News",
        url="https://news.ycombinator.com/rss",
        language="en",
        topics=["technology", "startup", "ai"],
        update_interval=1800,
    ),
    "mit_technology_review": RSSSource(
        id="mit_technology_review",
        name="MIT Technology Review",
        url="https://www.technologyreview.com/feed/",
        language="en",
        topics=["technology", "innovation", "ai"],
        update_interval=3600,
    ),
    "sspai": RSSSource(
        id="sspai",
        name="少数派",
        url="https://sspai.com/feed",
        language="zh",
        topics=["technology", "productivity", "lifestyle"],
        update_interval=3600,
    ),
    "arxiv_ai": RSSSource(
        id="arxiv_ai",
        name="arXiv cs.AI",
        url="http://export.arxiv.org/rss/cs.AI",
        language="en",
        topics=["ai", "research", "machine_learning"],
        update_interval=43200,
    ),
    "arxiv_cl": RSSSource(
        id="arxiv_cl",
        name="arXiv cs.CL",
        url="http://export.arxiv.org/rss/cs.CL",
        language="en",
        topics=["nlp", "llm", "research"],
        update_interval=43200,
    ),
    "baidu": RSSSource(
        id="baidu",
        name="百度实时热点",
        url="https://rss.aishort.top/?type=baidu",
        language="zh",
        topics=["news", "trending"],
        update_interval=3600,
    ),
    "guokr": RSSSource(
        id="guokr",
        name="果壳网(奇思妙想)",
        url="https://rss.aishort.top/?type=guokr",
        language="zh",
        topics=["science", "technology", "culture"],
        update_interval=3600,
    ),
}

AVAILABLE_RSS_SOURCES = "; ".join([f"{source.id}: {source.name}" for source in RSS_SOURCES.values()])
