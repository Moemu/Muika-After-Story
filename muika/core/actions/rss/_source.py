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
    # "arxiv_cs_ai": RSSSource(
    #     id="arxiv_cs_ai",
    #     name="arXiv CS.AI",
    #     url="http://export.arxiv.org/rss/cs.AI",
    #     language="en",
    #     topics=["ai", "research"],
    #     update_interval=7200,
    # ),
    # "arxiv_cs_cl": RSSSource(
    #     id="arxiv_cs_cl",
    #     name="arXiv CS.CL",
    #     url="http://export.arxiv.org/rss/cs.CL",
    #     language="en",
    #     topics=["computational linguistics", "nlp", "research"],
    #     update_interval=7200,
    # ),
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
}

AVAILABLE_RSS_SOURCES = "; ".join([f"{source.id}: {source.name}" for source in RSS_SOURCES.values()])
