"""Web research guard, fetch and service — no real network: MockTransport and fake resolvers only."""
from __future__ import annotations

import httpcore
import httpx
import pytest

from src.capability.web_research import guard
from src.capability.web_research.extract import Crawl4AIExtractor, Extracted, Extractor, sanitize
from src.capability.web_research.fetch import FetchRefused, fetch_page
from src.capability.web_research.guard import BlockedDestination, PublicOnlyBackend, check_url, is_public_address
from src.capability.web_research.service import ResearchUnavailable, research, research_many, web_research_settings


# ── URL policy ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", ["https://example.com/", "http://example.com/a?b=1", "https://sub.example.org:443/x"])
def test_public_web_urls_pass(url):
    assert check_url(url)


@pytest.mark.parametrize("url, reason", [
    ("file:///etc/passwd", "scheme"), ("ftp://example.com/", "scheme"), ("gopher://example.com/", "scheme"),
    ("http://127.0.0.1/", "literal ip"), ("http://169.254.169.254/latest/", "literal ip"), ("http://[::1]/", "literal ip"),
    ("http://10.0.0.1/", "literal ip"), ("http://localhost/", "local hostname"), ("http://printer.local/", "local"),
    ("http://metadata.internal/", "local"), ("http://intranet/", "single-label"), ("http://example.com:8080/", "port"),
    ("https://u:p@example.com/", "credentials"), ("", "missing"), ("http://" + "a" * 2100 + ".com/", "too long"),
])
def test_everything_else_is_refused_before_any_request(url, reason):
    with pytest.raises(BlockedDestination) as refused:
        check_url(url)
    assert reason.split()[0] in refused.value.reason


@pytest.mark.parametrize("address, public", [
    ("93.184.215.14", True), ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
    ("127.0.0.1", False), ("10.1.2.3", False), ("172.16.0.1", False), ("192.168.1.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("224.0.0.1", False),
    ("::1", False), ("fe80::1", False), ("fc00::1", False), ("::ffff:10.0.0.1", False), ("::ffff:127.0.0.1", False),
    ("not-an-ip", False),
])
def test_only_global_addresses_are_public(address, public):
    assert is_public_address(address) is public


# ── Connect-time enforcement (DNS rebinding, redirects to names that point inside) ──

class RecordingBackend(httpcore.AsyncNetworkBackend):
    def __init__(self) -> None:
        self.connected: list[tuple[str, int]] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.connected.append((host, port))
        raise httpcore.ConnectError("test backend: no real sockets")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no")

    async def sleep(self, seconds):
        return None


async def test_a_name_resolving_inside_the_network_is_refused_at_connect_time():
    async def resolver(host, port):
        raise BlockedDestination("resolves to a non-public address")

    inner = RecordingBackend()
    with pytest.raises(httpcore.ConnectError, match="blocked"):
        await PublicOnlyBackend(inner, resolver).connect_tcp("rebind.example.com", 443)
    assert inner.connected == []


async def test_a_public_name_connects_to_the_address_that_was_checked():
    async def resolver(host, port):
        return ["93.184.215.14"]

    inner = RecordingBackend()
    with pytest.raises(httpcore.ConnectError, match="test backend"):
        await PublicOnlyBackend(inner, resolver).connect_tcp("example.com", 443)
    assert inner.connected == [("93.184.215.14", 443)]


async def test_other_ports_and_unix_sockets_never_connect():
    inner = RecordingBackend()
    backend = PublicOnlyBackend(inner, lambda h, p: ["93.184.215.14"])
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("example.com", 6379)
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_unix_socket("/var/run/docker.sock")
    assert inner.connected == []


async def test_the_guarded_transport_refuses_a_private_resolution_end_to_end():
    async def resolver(host, port):
        raise BlockedDestination("resolves to a non-public address")

    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://looks-public.example.com/", respect_robots=False,
                         transport_factory=lambda: guard.guarded_transport(resolver=resolver, inner=RecordingBackend()))
    assert refused.value.code == "blocked_destination"


# ── Fetch rules (transport mocked) ───────────────────────────────────────────

HTML = "<html><head><title>Página</title></head><body><h1>Olá</h1></body></html>"


def site(routes: dict):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        route = routes.get(str(request.url))
        if route is None:
            return httpx.Response(404)
        return route() if callable(route) else route

    return (lambda: httpx.MockTransport(handler)), calls


async def test_a_page_is_fetched_and_decoded():
    factory, _ = site({"https://example.com/robots.txt": httpx.Response(404),
                       "https://example.com/": httpx.Response(200, html=HTML)})
    page = await fetch_page("https://example.com/", transport_factory=factory)
    assert page.status == 200 and "Olá" in page.html and page.redirects == 0 and page.content_type == "text/html"


async def test_redirects_are_followed_by_hand_and_each_target_is_checked():
    factory, calls = site({
        "https://example.com/a": httpx.Response(302, headers={"location": "/b"}),
        "https://example.com/b": httpx.Response(301, headers={"location": "https://example.org/c"}),
        "https://example.org/c": httpx.Response(200, html=HTML)})
    page = await fetch_page("https://example.com/a", transport_factory=factory, respect_robots=False)
    assert page.final_url == "https://example.org/c" and page.redirects == 2
    assert calls == ["https://example.com/a", "https://example.com/b", "https://example.org/c"]


@pytest.mark.parametrize("location", ["http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data/",
                                      "http://localhost:8000/", "file:///C:/Windows/win.ini", "http://intranet/"])
async def test_a_redirect_into_the_network_is_refused(location):
    factory, calls = site({"https://example.com/": httpx.Response(302, headers={"location": location})})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False)
    assert refused.value.code == "blocked_destination" and calls == ["https://example.com/"]


async def test_too_many_redirects_stop():
    factory, _ = site({"https://example.com/": httpx.Response(302, headers={"location": "https://example.com/"})})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False, max_redirects=3)
    assert refused.value.code == "too_many_redirects"


async def test_robots_txt_is_honoured():
    factory, calls = site({"https://example.com/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private"),
                           "https://example.com/private/x": httpx.Response(200, html=HTML)})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/private/x", transport_factory=factory)
    assert refused.value.code == "robots_disallowed" and calls == ["https://example.com/robots.txt"]


@pytest.mark.parametrize("ctype", ["application/pdf", "image/png", "application/octet-stream", ""])
async def test_only_text_pages_are_read(ctype):
    factory, _ = site({"https://example.com/": httpx.Response(200, content=b"x", headers={"content-type": ctype})})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False)
    assert refused.value.code == "unsupported_type"


async def test_a_body_over_the_limit_is_cut_off_whether_declared_or_not():
    big = "<p>" + "x" * 5000 + "</p>"
    factory, _ = site({"https://example.com/": httpx.Response(200, html=big)})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False, max_bytes=1000)
    assert refused.value.code == "too_large"

    async def chunks():
        yield b"x" * 600
        yield b"y" * 600

    def undeclared():  # streamed, no Content-Length: only the running count can stop it
        return httpx.Response(200, headers={"content-type": "text/html"}, content=chunks())

    factory, _ = site({"https://example.com/": undeclared})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False, max_bytes=1000)
    assert refused.value.code == "too_large"


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_http_errors_are_refusals(status):
    factory, _ = site({"https://example.com/": httpx.Response(status)})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False)
    assert refused.value.code == "http_error"


# ── Extraction and service ───────────────────────────────────────────────────

class FakeExtractor(Extractor):
    name = "fake"

    def __init__(self, markdown="# Olá\n", available=True):
        self._markdown, self._available, self.calls = markdown, available, 0

    def available(self):
        return self._available

    async def extract(self, html, url):
        self.calls += 1
        return Extracted(title="Página\x07", markdown=self._markdown, links_internal=1, links_external=2)


async def _fake_fetch(url, **kwargs):
    from src.capability.web_research.fetch import FetchedPage

    return FetchedPage(url=url, final_url=url, status=200, content_type="text/html", html=HTML, size_bytes=len(HTML),
                       redirects=0, seconds=0.01)


async def test_research_returns_clean_bounded_markdown(monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_MAX_MARKDOWN_CHARS", "20")
    web_research_settings.cache_clear()
    try:
        result = await research("https://example.com/", extractor=FakeExtractor("# Título\n" + "a" * 100 +
                                                                                "[x](javascript:alert(1))"),
                                fetcher=_fake_fetch)
    finally:
        web_research_settings.cache_clear()
    assert result.truncated is True and len(result.markdown) == 20 and result.title == "Página"


def test_sanitize_neutralises_script_links_and_control_characters():
    assert sanitize("a\x00b [x](javascript:alert(1)) [y](data:text/html,1)", 1000) == "ab [x](#) [y](#)"


async def test_without_an_extractor_the_capability_is_off():
    with pytest.raises(ResearchUnavailable):
        await research("https://example.com/", extractor=FakeExtractor(available=False), fetcher=_fake_fetch)


async def test_research_many_is_capped_and_one_refusal_does_not_stop_the_rest():
    async def fetcher(url, **kwargs):
        if "bad" in url:
            raise FetchRefused("blocked_destination")
        return await _fake_fetch(url)

    urls = ["https://a.example/", "https://bad.example/", "https://c.example/", "https://d.example/"]
    results = await research_many(urls, extractor=FakeExtractor(), fetcher=fetcher)
    assert len(results) == web_research_settings().web_research_max_pages == 3
    assert isinstance(results[1], FetchRefused) and results[0].title == "Página"


def test_the_crawl4ai_extractor_is_off_without_its_interpreter_and_inherits_no_secrets(monkeypatch):
    assert Crawl4AIExtractor("").available() is False
    assert Crawl4AIExtractor(r"C:\does\not\exist\python.exe").available() is False
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-reach-the-worker")
    monkeypatch.setenv("HIGGSFIELD_API_KEY", "id:secret")
    env = Crawl4AIExtractor("python")._env()
    assert "OPENAI_API_KEY" not in env and "HIGGSFIELD_API_KEY" not in env
    assert not any("secret" in value or "sk-" in value for value in env.values())


def test_the_worker_carries_the_crawl4ai_attribution_and_disables_sockets():
    from src.capability.web_research.extract import WORKER

    source = WORKER.read_text(encoding="utf-8")
    assert "This product includes software developed by UncleCode" in source
    assert "socket.socket.connect = _no_network" in source


# ── The capability contract (CP6) ────────────────────────────────────────────

from src.capability.web_research.capability import WebResearchCapability, host_policy  # noqa: E402
from src.capability.web_research.contract import (  # noqa: E402
    REFUSAL_CODES,
    WebResearchDisabled,
    WebResearchInvalid,
    WebResearchRateLimited,
    WebResearchRequest,
)


@pytest.fixture
def research_on(monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_ENABLED", "true")
    monkeypatch.setenv("WEB_RESEARCH_ALLOWED_DOMAINS", "")
    monkeypatch.setenv("WEB_RESEARCH_BLOCKED_DOMAINS", "")
    monkeypatch.setenv("WEB_RESEARCH_RATE_PER_MINUTE", "10")
    web_research_settings.cache_clear()
    from src.capability.web_research import capability

    capability._limiter._calls.clear()
    yield
    web_research_settings.cache_clear()
    capability._limiter._calls.clear()


async def test_the_capability_is_off_by_default(monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_ENABLED", "false")
    web_research_settings.cache_clear()
    try:
        with pytest.raises(WebResearchDisabled):
            await WebResearchCapability(extractor=FakeExtractor(), fetcher=_fake_fetch).run(
                WebResearchRequest(urls=("https://example.com/",)), actor_id="u1")
    finally:
        web_research_settings.cache_clear()


async def test_on_but_without_an_extractor_it_is_still_off(research_on):
    with pytest.raises(WebResearchDisabled):
        await WebResearchCapability(extractor=FakeExtractor(available=False), fetcher=_fake_fetch).run(
            WebResearchRequest(urls=("https://example.com/",)), actor_id="u1")


async def test_pages_and_refusals_come_back_in_the_contract_shape(research_on):
    async def fetcher(url, **kwargs):
        if "blocked" in url:
            raise FetchRefused("blocked_destination", "resolves to 10.0.0.1 via C:\internal")
        return await _fake_fetch(url)

    response = await WebResearchCapability(extractor=FakeExtractor(), fetcher=fetcher).run(
        WebResearchRequest(urls=("https://a.example/", "https://blocked.example/", "https://a.example/")), actor_id="u1")
    assert [p.url for p in response.pages] == ["https://a.example/"]  # the duplicate is read once
    assert [(r.url, r.code) for r in response.refused] == [("https://blocked.example/", "blocked_destination")]
    assert all(r.code in REFUSAL_CODES for r in response.refused)
    text = repr(response).lower()
    assert "10.0.0.1" not in text and "c:\internal" not in text and "crawl4ai" not in text
    assert "fakeextractor" not in text


async def test_page_and_character_limits_never_exceed_the_operator_ceiling(research_on, monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_MAX_PAGES", "2")
    monkeypatch.setenv("WEB_RESEARCH_MAX_MARKDOWN_CHARS", "5")
    web_research_settings.cache_clear()
    extractor = FakeExtractor("# Um texto longo")
    response = await WebResearchCapability(extractor=extractor, fetcher=_fake_fetch).run(
        WebResearchRequest(urls=tuple(f"https://p{i}.example/" for i in range(5)), max_pages=50,
                           max_chars_per_page=10_000), actor_id="u1")
    assert len(response.pages) == 2 and extractor.calls == 2
    assert all(len(p.content_markdown) == 5 and p.truncated for p in response.pages)


async def test_the_rate_limit_is_per_actor(research_on, monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_RATE_PER_MINUTE", "2")
    web_research_settings.cache_clear()
    now = [0.0]
    capability = WebResearchCapability(extractor=FakeExtractor(), fetcher=_fake_fetch, clock=lambda: now[0])
    request = WebResearchRequest(urls=("https://example.com/",))
    await capability.run(request, actor_id="u1")
    await capability.run(request, actor_id="u1")
    with pytest.raises(WebResearchRateLimited):
        await capability.run(request, actor_id="u1")
    await capability.run(request, actor_id="u2")  # another actor is unaffected
    now[0] = 61.0
    await capability.run(request, actor_id="u1")  # the window moved on


@pytest.mark.parametrize("request_", [WebResearchRequest(urls=()), WebResearchRequest(urls=("",)),
                                      WebResearchRequest(urls=(None,))])  # type: ignore[arg-type]
async def test_malformed_requests_are_refused(research_on, request_):
    with pytest.raises(WebResearchInvalid):
        await WebResearchCapability(extractor=FakeExtractor(), fetcher=_fake_fetch).run(request_, actor_id="u1")


def test_domain_policy_blocklist_wins_and_allowlist_restricts(research_on, monkeypatch):
    assert host_policy("example.com") is True
    monkeypatch.setenv("WEB_RESEARCH_BLOCKED_DOMAINS", "bad.example, .tracker.example")
    monkeypatch.setenv("WEB_RESEARCH_ALLOWED_DOMAINS", "example.com,gov.br")
    web_research_settings.cache_clear()
    assert host_policy("www.example.com") is True and host_policy("dados.gov.br") is True
    assert host_policy("other.org") is False
    assert host_policy("bad.example") is False and host_policy("a.tracker.example") is False
    assert host_policy("notexample.com") is False  # suffix match is by label, not by string


async def test_a_redirect_to_a_domain_outside_the_policy_is_refused(research_on, monkeypatch):
    monkeypatch.setenv("WEB_RESEARCH_ALLOWED_DOMAINS", "example.com")
    web_research_settings.cache_clear()
    factory, calls = site({"https://example.com/": httpx.Response(302, headers={"location": "https://evil.example/x"}),
                           "https://evil.example/x": httpx.Response(200, html=HTML)})
    with pytest.raises(FetchRefused) as refused:
        await fetch_page("https://example.com/", transport_factory=factory, respect_robots=False,
                         host_policy=host_policy)
    assert refused.value.code == "domain_not_allowed" and calls == ["https://example.com/"]
