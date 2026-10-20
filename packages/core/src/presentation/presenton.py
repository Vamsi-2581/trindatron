"""PresentonProvider — the only PresentationProvider today.

Written against a live self-hosted Presenton 0.9.10-beta, driven end to end
on 2026-09-11. Everything below reflects what the running service actually
does rather than what its reference says, because the two disagree in ways
that would have failed in production:

  auth      the docs describe Basic Auth; the service issues a session
            cookie from POST /api/v1/auth/login and rejects Basic with 401
  paths     generation is POST /api/v1/ppt/presentation/generate
  response  {"presentation_id", "path", "edit_path"} — `path` is a URL on
            the same host, fetched with the same session
  timing    synchronous, minutes long; a caller must treat it as a job

The Gemini key never reaches this service. Presenton is configured with
CUSTOM_LLM_URL pointing at TRINDATRON's own gateway and a local token, so
the Google credential stays on our side of the boundary — see
settings.presenton_llm_base_url.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import httpx

from src.presentation.provider import (
    ExportFormat,
    ImageStrategy,
    PresentationArtifact,
    PresentationError,
    PresentationProvider,
    PresentationQuotaError,
    PresentationRequest,
    PresentationResult,
    ProviderAvailability,
)

logger = logging.getLogger("trinity.presentation.presenton")

_MEDIA_TYPES = {
    ExportFormat.PPTX: (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ExportFormat.PDF: "application/pdf",
}

# Presenton reports a model-provider refusal as its own 429/503 with a
# generic message. Both mean "the model said no", and neither should be
# retried harder from here.
_QUOTA_STATUSES = frozenset({429, 503})


class PresentonProvider(PresentationProvider):
    name = "presenton"

    def __init__(
        self,
        *,
        base_url: str,
        username: str = "",
        password: str = "",
        api_key: str = "",
        timeout_s: float = 900.0,
        health_timeout_s: float = 5.0,
        wake_timeout_s: float = 180.0,
        wake_poll_interval_s: float = 3.0,
    ) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._username = username
        self._password = password
        self._api_key = api_key
        self._timeout_s = timeout_s
        self._health_timeout_s = health_timeout_s
        self._wake_timeout_s = wake_timeout_s
        self._wake_poll_interval_s = wake_poll_interval_s
        self._session_cookie: str | None = None
        self._lock = asyncio.Lock()

    # ── plumbing ─────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        return urljoin(self._base, path.lstrip("/"))

    def _auth_headers(self) -> dict[str, str]:
        """Bearer token when one is configured.

        Two auth shapes exist and the preferred one is the API key: it is
        issued per client, revocable from the admin endpoint without touching
        anyone else, and carries no password that also unlocks the engine's
        own UI. The session cookie remains as a fallback because it is what a
        freshly-installed engine has before any key has been minted, and
        losing that would make first-run setup impossible.
        """
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    async def _authenticate(self, client: httpx.AsyncClient) -> None:
        """Establish whatever credential this engine is configured for.

        A no-op when an API key is in use — a bearer token needs no handshake,
        which is the second reason to prefer it: one fewer round trip on a
        service that may have just cold-started.
        """
        if self._api_key:
            return
        await self._login(client)

    async def _login(self, client: httpx.AsyncClient) -> None:
        """Obtain a session cookie. Credentials are sent once, never logged."""
        r = await client.post(
            self._url("api/v1/auth/login"),
            json={"username": self._username, "password": self._password},
        )
        if r.status_code != 200:
            # The body can echo the submitted username; only the status is
            # recorded so a credential never reaches a log line.
            raise PresentationError(f"presentation engine rejected sign-in ({r.status_code})")
        cookie = SimpleCookie()
        for raw in r.headers.get_list("set-cookie"):
            cookie.load(raw)
        if "presenton_session" not in cookie:
            raise PresentationError("presentation engine returned no session")
        self._session_cookie = cookie["presenton_session"].value

    def _cookies(self) -> dict[str, str]:
        return {"presenton_session": self._session_cookie} if self._session_cookie else {}

    # ── contract ─────────────────────────────────────────────────────────

    async def availability(self) -> ProviderAvailability:
        """Never raises: an unreachable engine is an answer, not an error."""
        if not self._base:
            return ProviderAvailability(False, "not configured")
        # Either credential shape is enough; neither is not. Checked before
        # any network call so a misconfigured deployment says so instantly
        # rather than after a wake timeout.
        if not self._api_key and not (self._username and self._password):
            return ProviderAvailability(False, "not configured")
        try:
            async with httpx.AsyncClient(timeout=self._health_timeout_s) as client:
                r = await client.get(self._url("api/v1/auth/status"))
            if r.status_code != 200:
                return ProviderAvailability(False, f"health status {r.status_code}")
            if not r.json().get("configured"):
                return ProviderAvailability(False, "engine reports unconfigured")
            return ProviderAvailability(True)
        except Exception as exc:
            return ProviderAvailability(False, f"unreachable: {type(exc).__name__}")

    async def wait_until_ready(self) -> ProviderAvailability:
        """Wake a scaled-to-zero engine and wait for it to answer.

        Presenton is deployed scaled-to-zero (see infra/presenton/README.md):
        a 5.45 GB image doing occasional work is not worth paying for around
        the clock. The cost of that is a cold start on the first request
        after an idle period — the container has to boot before it can serve.

        Waiting here is affordable precisely because generation is already an
        async job: nobody is watching a spinner, and a cold start measured in
        tens of seconds sits inside a task that takes minutes regardless. The
        same wait inside an HTTP request/response cycle would be unacceptable,
        which is exactly why the capability was built as a job first.

        Polls rather than sleeping a fixed interval, so a warm engine returns
        immediately and costs nothing.
        """
        deadline = time.monotonic() + self._wake_timeout_s
        attempts = 0
        last = ProviderAvailability(False, "not probed")
        while time.monotonic() < deadline:
            attempts += 1
            last = await self.availability()
            if last.available:
                if attempts > 1:
                    logger.info(
                        "presentation: engine woke | attempts=%d waited_s=%.1f",
                        attempts, self._wake_timeout_s - (deadline - time.monotonic()),
                    )
                return last
            await asyncio.sleep(self._wake_poll_interval_s)
        logger.warning(
            "presentation: engine did not become ready | attempts=%d timeout_s=%.0f reason=%s",
            attempts, self._wake_timeout_s, last.reason,
        )
        return ProviderAvailability(False, f"not ready after {self._wake_timeout_s:.0f}s")

    def _same_origin_url(self, path: str) -> str:
        """Resolve an engine-supplied path against the engine's own origin,
        and refuse anything that lands elsewhere.

        The generate response hands back a `path` that we then fetch. Passing
        that straight to urljoin is an SSRF: urljoin("http://engine/", "http://
        elsewhere/x") returns "http://elsewhere/x", so an engine that is
        compromised, misconfigured, or simply pointed at a user-influenced
        value could make this process issue requests to arbitrary internal
        hosts — the cloud metadata endpoint being the classic target. Core
        reaches Presenton over private networking, which makes this process a
        better-placed attacker than the internet is.

        So the resolved URL is checked against the configured base: same
        scheme, same host, same port, or it is refused. A path is data from a
        service, not an instruction about where to connect.
        """
        resolved = urljoin(self._base, path.lstrip("/"))
        base_parts = urlsplit(self._base)
        target = urlsplit(resolved)
        if (target.scheme, target.hostname, target.port) != (
            base_parts.scheme,
            base_parts.hostname,
            base_parts.port,
        ):
            raise PresentationError("engine returned an artifact path outside its own origin")
        return resolved

    async def generate(self, request: PresentationRequest) -> PresentationResult:
        payload = {
            "content": request.content,
            "n_slides": request.n_slides,
            "language": request.language,
            "template": "general",
            "export_as": request.export_as.value,
            "web_search": request.web_search,
            "include_title_slide": request.include_title_slide,
        }
        if request.instructions:
            payload["instructions"] = request.instructions
        if request.source_files:
            payload["files"] = list(request.source_files)

        started = time.monotonic()
        async with self._lock:  # one deck at a time per provider instance
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                await self._authenticate(client)
                r = await client.post(
                    self._url("api/v1/ppt/presentation/generate"),
                    json=payload,
                    cookies=self._cookies(),
                    headers=self._auth_headers(),
                )

                if r.status_code in _QUOTA_STATUSES:
                    # Measured: the free Gemini tier caps at 20 requests per
                    # DAY, and one deck needs more than that. Retrying here
                    # would burn the remainder and lengthen the outage.
                    raise PresentationQuotaError(
                        "the presentation engine's model provider is rate limited"
                    )
                if r.status_code != 200:
                    raise PresentationError(f"generation failed ({r.status_code})")

                body = r.json()
                path = body.get("path")
                if not path:
                    raise PresentationError("generation returned no artifact path")

                artifact_url = self._same_origin_url(path)
                fetched = await client.get(
                    artifact_url, cookies=self._cookies(), headers=self._auth_headers()
                )
                if fetched.status_code != 200:
                    raise PresentationError(
                        f"artifact fetch failed ({fetched.status_code})"
                    )
                data = fetched.content

        fmt = request.export_as
        filename = f"presentation.{fmt.value}"
        logger.info(
            "presentation generated | provider=%s id=%s format=%s bytes=%d duration_s=%.1f",
            self.name, body.get("presentation_id"), fmt.value, len(data),
            time.monotonic() - started,
        )
        return PresentationResult(
            provider=self.name,
            external_id=body.get("presentation_id"),
            artifacts=[PresentationArtifact(filename, _MEDIA_TYPES[fmt], data)],
            duration_s=time.monotonic() - started,
        )


def image_provider_env(strategy: ImageStrategy) -> dict[str, str]:
    """The engine-side image settings a strategy implies.

    Kept beside the provider because it is Presenton's vocabulary, not
    TRINDATRON's — the capability speaks in ImageStrategy and this is the
    single place that translates.

    IMAGE_PROVIDER is process-wide environment, not a request field, so this
    choice is made per DEPLOYMENT, not per deck. STOCK_FIRST and STOCK_ONLY
    therefore both resolve to stock: "stock first, generate only where stock
    fails" is a per-slide decision and the engine has no per-slide control to
    express it. The cheaper half of that strategy is real, the selective half
    is not, and pretending otherwise would silently bill generated images at
    $0.151 each — see src/presentation/image_policy.py, which makes the
    whole-deck choice this function then encodes.

    ENABLE_PARALLEL_IMAGE_GENERATION is forced off. It defaults to true, and
    parallelism is exactly what broke during the audit: six concurrent image
    calls against the model provider produced 0/6 successes, where the same
    six issued in sequence produced 5/6. A rate limit is not overcome by
    asking faster.
    """
    if strategy is ImageStrategy.NONE:
        return {"DISABLE_IMAGE_GENERATION": "true"}
    if strategy is ImageStrategy.GENERATED:
        return {
            "IMAGE_PROVIDER": "gemini_flash",
            "DISABLE_IMAGE_GENERATION": "false",
            "ENABLE_PARALLEL_IMAGE_GENERATION": "false",
        }
    # STOCK_FIRST and STOCK_ONLY both land on stock photography today.
    return {
        "IMAGE_PROVIDER": "pexels",
        "DISABLE_IMAGE_GENERATION": "true",
        "ENABLE_PARALLEL_IMAGE_GENERATION": "false",
    }


def engine_environment(
    *,
    strategy: ImageStrategy,
    llm_base_url: str,
    llm_gateway_token: str,
    model: str,
    pexels_api_key: str = "",
    web_search: bool = True,
) -> dict[str, str]:
    """The complete environment the Presenton container is deployed with.

    This is the deployable artifact for the engine service, expressed as code
    so it can be asserted on rather than living only in a dashboard someone
    edits by hand. infra/presenton/README.md renders it; the tests pin the
    parts that are load-bearing.

    Why LLM=custom and not LLM=google
    ---------------------------------
    Pointing the engine at Google directly would mean handing a third-party
    container our Gemini key, and it would leave the engine free to fire slide
    generations in parallel with no way for us to pace or count them. LLM=custom
    aims it at TRINDATRON's own gateway instead: the Google credential stays on
    our side, we see every call, and the token the engine holds is one we can
    rotate without touching Google.

    The model-name trap this function exists to close
    ------------------------------------------------
    Presenton's GOOGLE_MODEL defaults to `models/gemini-2.0-flash`, and its
    image setting `gemini_flash` is a DIFFERENT model again — Google's image
    model, not the text one. So "Gemini Flash" in the engine's own UI means
    neither of the things a reader assumes and certainly not the model
    TRINDATRON selected. CUSTOM_MODEL is therefore set explicitly from
    settings.presentation_model, and nothing is left to an engine default.

    Web search stays native (WEB_SEARCH_PROVIDER=native): grounding is the
    model's own, so no Tavily/Exa/Brave/Serper key exists to leak, no second
    vendor is added, and the cost is Google's published per-search rate rather
    than another subscription.
    """
    env: dict[str, str] = {
        # ── Intelligence: Gemini, reached through our own gateway ────────
        "LLM": "custom",
        "CUSTOM_LLM_URL": llm_base_url,
        "CUSTOM_LLM_API_KEY": llm_gateway_token,
        "CUSTOM_MODEL": model,
        # ── Grounding: the model's own, no third-party search vendor ─────
        "WEB_GROUNDING": "true" if web_search else "false",
        "WEB_SEARCH_PROVIDER": "native",
        # ── Hardening ────────────────────────────────────────────────────
        # The engine ships a UI that can rewrite provider keys. This service
        # is reached only by TRINDATRON over private networking and must not
        # be reconfigurable from a browser session.
        "CAN_CHANGE_KEYS": "false",
    }
    env.update(image_provider_env(strategy))
    if pexels_api_key:
        env["PEXELS_API_KEY"] = pexels_api_key
    return env
