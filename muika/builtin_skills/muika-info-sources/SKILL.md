---
name: muika-info-sources
description: Guide for checking Muika's own information sources on demand — her RSS subscriptions and where they are defined.
---

# muika-info-sources

Your regular reading arrives through the digest pipeline, but you can also check your
sources yourself at any time.

## RSS subscriptions

Your subscriptions are defined in `muika/core/actions/tools/rss/_source.py`. Read that
file with read_file for the current list — each entry gives an id, name, url, language
and topics. Only sources marked `digest=True` are summarized for you automatically;
to read any source yourself, fetch its url with fetch_web_content.
