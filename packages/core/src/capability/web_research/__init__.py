"""Web research: fetch one public page safely and turn it into clean text for an agent. Internal, not wired yet.

    guard.py     what may be fetched, and a network layer that refuses private destinations at connect time
    fetch.py     one guarded fetch: redirects re-checked hop by hop, size/time/type limits, robots.txt
    extract.py   HTML -> markdown/structure; Crawl4AI runs out of process with no network (optional)
    service.py   research(url): guard -> fetch -> extract, bounded, with a trace for logs

Nothing here reaches a user yet, and nothing here is a Core dependency: without an
extractor configured the capability reports itself unavailable.
"""
