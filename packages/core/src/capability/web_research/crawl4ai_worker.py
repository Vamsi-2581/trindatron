"""Crawl4AI extraction worker — run by Crawl4AI's own isolated interpreter, never imported by the Core.

    <crawl4ai venv>/python -I crawl4ai_worker.py  < {"url": ..., "html": ...}  > {"title": ..., "markdown": ...}

It never touches the network: sockets are disabled before Crawl4AI is imported, and
only the offline scraping and markdown steps are used (the Core fetched the page).

This product includes software developed by UncleCode (https://x.com/unclecode) as part
of the Crawl4AI project (https://github.com/unclecode/crawl4ai).
"""
import json
import socket
import sys


def _no_network(*args, **kwargs):
    raise OSError("network disabled in the extraction worker")


socket.socket.connect = _no_network  # type: ignore[method-assign]
socket.socket.connect_ex = _no_network  # type: ignore[method-assign]
socket.create_connection = _no_network  # type: ignore[assignment]
socket.getaddrinfo = _no_network  # type: ignore[assignment]


def main() -> int:
    request = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    html, url = str(request.get("html") or ""), str(request.get("url") or "")

    from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    scraped = LXMLWebScrapingStrategy().scrap(url, html)
    cleaned = scraped.cleaned_html or ""
    markdown = DefaultMarkdownGenerator().generate_markdown(cleaned, base_url=url, citations=False)
    links = scraped.links.model_dump() if hasattr(scraped.links, "model_dump") else dict(scraped.links or {})
    metadata = scraped.metadata or {}
    out = {
        "title": str(metadata.get("title") or "")[:300],
        "markdown": markdown.raw_markdown or "",
        "links_internal": len(links.get("internal") or []),
        "links_external": len(links.get("external") or []),
    }
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
