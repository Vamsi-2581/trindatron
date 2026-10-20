"""The LLM gateway the presentation engine talks to.

Presenton is configured with LLM=custom and CUSTOM_LLM_URL pointing here,
not at Google. Everything in this module exists because of that one decision,
which buys four things that pointing it at Google directly would lose:

  the key stays ours     A third-party container never holds the Gemini
                         credential. It holds a token we mint and can rotate
                         without touching Google.
  the model is ours      The engine's requested model is DISCARDED and
                         replaced with settings.presentation_model. Presenton
                         defaults GOOGLE_MODEL to models/gemini-2.0-flash and
                         calls its image model "gemini_flash"; neither is what
                         TRINDATRON selected, and neither gets to win here.
                         This is also what makes "Gemini only" enforceable
                         rather than merely configured — there is no request
                         body that can reach another provider through this
                         endpoint.
  we can count           Presenton reports no token usage. Google does, on
                         every response, and this is the only place both are
                         visible — so real cost is recorded here and the job
                         finalizes against measurement instead of estimate.
  we can pace            The engine fires slide generations with no throttle
                         of its own. One semaphore here bounds concurrency
                         against the provider, which is the difference the
                         audit measured between 5/6 calls succeeding and 0/6.

Exposure
--------
Mounted under /internal, authenticated by a shared token, and reached over
private networking only. It is not a user-facing endpoint, takes no user
authentication, and must never be advertised in the public schema —
include_in_schema=False on the route is part of that, not decoration.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session as DBSession

from src.config import settings
from src.database import SessionLocal as _real_SessionLocal
from src.database import get_db
from src.models.presentation_llm_call import PresentationLlmCall
from src.observability.metrics import core_metrics
from src.presentation.cost import GEMINI_IN_PER_M, GEMINI_OUT_PER_M

logger = logging.getLogger("trinity.presentation.gateway")

router = APIRouter(prefix="/internal/presenton", tags=["internal"])

_gateway_calls = core_metrics.counter(
    "presentation_gateway_calls_total",
    "Model calls proxied for the presentation engine, labeled by outcome.",
)

# Google's OpenAI-compatible surface. The engine speaks OpenAI's wire format,
# Google accepts it here, so no translation layer is needed in between.
GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"

#: Upstream answers that mean "busy right now", not "wrong request". Google
#: answers 503 "This model is currently experiencing high demand" in spikes
#: (seen in production 2026-09-30); a deck is many calls, and giving up on the
#: first one failed the whole deck. 429 is deliberately absent: a spent quota
#: is not helped by asking again, and PresentonProvider treats it as final.
RETRYABLE_UPSTREAM = frozenset({500, 502, 503, 504})
#: Waits before each retry. Only before the first relayed byte, so the engine never sees a retried call.
UPSTREAM_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 5.0, 10.0)

# Bounded concurrency against the provider. Presenton parallelises slide
# generation and has no setting to stop it; this is where that is actually
# constrained. One in flight is deliberate rather than cautious — the audit
# measured six concurrent calls returning 0/6 successes against five of six
# succeeding in sequence.
_MAX_CONCURRENT_CALLS = 1

# Overridable session factory, same contract execution_service documents:
# the streaming path writes after the request-scoped session is gone, so it
# needs its own, and tests point this ONE name at their engine.
_session_factory = _real_SessionLocal


def _require_gateway_token(authorization: str | None = Header(default=None)) -> None:
    """Shared-token auth for a service-to-service endpoint.

    Compared with compare_digest: a token check that short-circuits on the
    first wrong byte leaks its length and prefix to anyone who can time it,
    and this endpoint is reachable from inside the private network.
    """
    expected = settings.presenton_llm_gateway_token
    if not expected:
        # No token configured means the gateway was never meant to be on.
        # Closed, not open — an unauthenticated proxy to a paid model API is
        # the worst possible default.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    presented = ""
    if authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()
    # Bytes, not str: compare_digest raises on a non-ASCII str, which turned a
    # wrong token with an accent in it into a 500 instead of a 401.
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized"
        )


def _usd_for(input_tokens: int, output_tokens: int, reasoning_tokens: int) -> float:
    """Reasoning is billed as output — see cost.py. Folding it in at the input
    rate would understate a thinking model's cost by roughly five times."""
    billed_output = output_tokens + reasoning_tokens
    return input_tokens / 1e6 * GEMINI_IN_PER_M + billed_output / 1e6 * GEMINI_OUT_PER_M


@router.post(
    "/llm/v1/chat/completions",
    include_in_schema=False,
    dependencies=[Depends(_require_gateway_token)],
)
async def chat_completions(request: Request, db: DBSession = Depends(get_db)) -> dict:
    """Proxy one chat completion to Gemini, and record what it cost."""
    if not settings.gemini_api_key:
        # The engine cannot fix this and neither can a retry.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Presentation intelligence is not configured.",
        )

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")

    # The engine's choice of model is discarded, not validated. Validation
    # would imply there is a set of acceptable answers; there is exactly one,
    # and it comes from our configuration.
    requested = body.get("model")
    body["model"] = settings.presentation_model
    if requested and requested != settings.presentation_model:
        logger.info(
            "presentation gateway: overrode engine model | requested=%s used=%s",
            requested, settings.presentation_model,
        )
    # What the engine actually asked for, minus the prompt itself. Slide
    # generation depends on structured output and streaming, and a mismatch
    # in either produces an empty outline rather than an error — which is
    # exactly what a first integration run hits. Keys only: the messages
    # carry user content and never belong in a log.
    logger.debug(
        "presentation gateway: request shape | keys=%s stream=%s response_format=%s tools=%s",
        sorted(k for k in body if k != "messages"),
        body.get("stream"),
        (body.get("response_format") or {}).get("type") if isinstance(body.get("response_format"), dict) else body.get("response_format"),
        bool(body.get("tools")),
    )

    # Presenton STREAMS. Measured live on 2026-09-11: it sends
    # stream=true, stream_options, and response_format=json_schema. An
    # earlier version of this gateway stripped `stream` to keep the usage
    # totals visible, and the failure mode was silent and expensive — Google
    # answered 200 and billed 436 output tokens, the engine's SSE parser
    # accumulated nothing from a non-streamed body, and generation died with
    # "Expecting value: line 1 column 1" on an empty outline. A 400 that
    # costs a real model call is the worst shape a bug can take.
    #
    # So streaming is proxied through, and usage is taken from the final
    # chunk instead. include_usage is forced on rather than trusted: without
    # it Google sends no usage block at all and the deck would finalize
    # against its estimate forever.
    if bool(body.get("stream")):
        options = body.get("stream_options")
        body["stream_options"] = {
            **(options if isinstance(options, dict) else {}),
            "include_usage": True,
        }
        return StreamingResponse(
            _proxy_stream(body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    started = time.monotonic()
    async with _call_semaphore():
        try:
            async with httpx.AsyncClient(timeout=settings.presenton_timeout_s) as client:
                for attempt, delay in enumerate((*UPSTREAM_RETRY_DELAYS_S, None)):
                    upstream = await client.post(
                        f"{GEMINI_OPENAI_BASE}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {settings.gemini_api_key}"},
                    )
                    if upstream.status_code not in RETRYABLE_UPSTREAM or delay is None:
                        break
                    logger.info("presentation gateway: upstream %s, retrying in %.0fs | attempt=%d",
                                upstream.status_code, delay, attempt + 1)
                    await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            _gateway_calls.inc(outcome="transport_error")
            logger.warning("presentation gateway: upstream unreachable | %s", type(exc).__name__)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Presentation intelligence is unreachable.",
            )

    if upstream.status_code == 429:
        # Passed through unchanged so the engine surfaces it and
        # PresentonProvider can classify it as a quota refusal rather than a
        # transient error worth retrying.
        _gateway_calls.inc(outcome="rate_limited")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Presentation intelligence is rate limited.",
        )
    if upstream.status_code != 200:
        _gateway_calls.inc(outcome="upstream_error")
        # Status only. An upstream error body can echo the prompt back.
        logger.warning("presentation gateway: upstream %s", upstream.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Presentation intelligence returned an error.",
        )

    payload = upstream.json()
    # Same extraction and same reconstruction the streaming path uses — one
    # function, so the two paths cannot drift into billing differently.
    input_tokens, output_tokens, reasoning_tokens = _usage_from(payload.get("usage") or {})
    cost_usd = _record_call(input_tokens, output_tokens, reasoning_tokens, db)

    _gateway_calls.inc(outcome="ok")
    logger.info(
        "presentation gateway: call | in=%d out=%d reasoning=%d usd=%.6f duration_s=%.1f",
        input_tokens, output_tokens, reasoning_tokens, cost_usd, time.monotonic() - started,
    )
    return payload


def _usage_from(usage: dict) -> tuple[int, int, int]:
    """(input, output, reasoning) from an OpenAI-shaped usage block.

    Google's OpenAI-compatible endpoint frequently reports thinking tokens in
    NEITHER completion_tokens NOR completion_tokens_details — they appear only
    in the total. Measured live on 2026-09-11: prompt_tokens=10,
    completion_tokens=0, reasoning_tokens absent, total_tokens=58. Those 48
    tokens are real and billed at the output rate, so trusting the explicit
    fields alone under-reports that call by roughly 25x.

    The remainder is reconstructed from the total. Reconstruction can only
    ever ADD cost that was otherwise ignored, never remove any, so the failure
    mode is billing accurately rather than over-billing.
    """
    input_tokens = int(usage.get("prompt_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or 0)
    total_tokens = int(usage.get("total_tokens") or 0)
    details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = int(details.get("reasoning_tokens") or 0)
    if total_tokens > 0:
        unexplained = total_tokens - input_tokens - output_tokens - reasoning_tokens
        if unexplained > 0:
            reasoning_tokens += unexplained
    return input_tokens, output_tokens, reasoning_tokens


def _record_call(
    input_tokens: int, output_tokens: int, reasoning_tokens: int,
    db: DBSession | None = None,
) -> float:
    """Persist one call's measured cost.

    `db` is the request-scoped session when there is one. The streaming path
    has none it can safely use — a StreamingResponse outlives the request, so
    writing through that session would land in a closed transaction — and
    passes None, which opens one from _session_factory instead.

    _session_factory is the same single overridable name execution_service
    uses for exactly this reason: background work that outlives a request
    cannot receive a session through Depends(get_db), and tests need one
    place to point it at their own engine.
    """
    cost_usd = _usd_for(input_tokens, output_tokens, reasoning_tokens)
    owned = db is None
    db = db or _session_factory()
    try:
        db.add(PresentationLlmCall(
            model=settings.presentation_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            cost_usd=cost_usd,
        ))
        db.commit()
    except Exception:
        # Never fail the engine's call because bookkeeping failed — the deck
        # is already paid for upstream. The job falls back to its pre-flight
        # estimate when it finds fewer rows than calls, which errs high.
        db.rollback()
        logger.exception("presentation gateway: failed recording usage")
    finally:
        if owned:
            db.close()
    return cost_usd


async def _proxy_stream(body: dict):
    """Relay the upstream SSE verbatim, reading usage out of it on the way past.

    Verbatim matters: the engine parses these chunks with its own OpenAI
    client, so any reformatting here becomes a parse failure there. The only
    thing this does besides copy bytes is watch for the usage block, which
    arrives in the final chunk when include_usage is set.
    """
    started = time.monotonic()
    usage: dict = {}
    async with _call_semaphore():
        try:
            async with httpx.AsyncClient(timeout=settings.presenton_timeout_s) as client:
                for attempt, delay in enumerate((*UPSTREAM_RETRY_DELAYS_S, None)):
                    async with client.stream(
                        "POST",
                        f"{GEMINI_OPENAI_BASE}/chat/completions",
                        json=body,
                        headers={"Authorization": f"Bearer {settings.gemini_api_key}"},
                    ) as upstream:
                        # Nothing has been relayed yet, so an overloaded model
                        # can still be retried invisibly to the engine.
                        if upstream.status_code in RETRYABLE_UPSTREAM and delay is not None:
                            await upstream.aread()
                            logger.info("presentation gateway: upstream %s (stream), retrying in %.0fs | attempt=%d",
                                        upstream.status_code, delay, attempt + 1)
                            retry = True
                        else:
                            retry = False
                            async for piece in _relay(upstream, usage):
                                yield piece
                            if upstream.status_code != 200:
                                return
                    if not retry:
                        break
                    await asyncio.sleep(delay)
        except Exception as exc:  # noqa: BLE001
            _gateway_calls.inc(outcome="transport_error")
            logger.warning(
                "presentation gateway: upstream unreachable (stream) | %s", type(exc).__name__
            )
            yield _sse_error(502)
            return

    input_tokens, output_tokens, reasoning_tokens = _usage_from(usage)
    cost_usd = _record_call(input_tokens, output_tokens, reasoning_tokens)
    _gateway_calls.inc(outcome="ok")
    logger.info(
        "presentation gateway: streamed call | in=%d out=%d reasoning=%d usd=%.6f duration_s=%.1f",
        input_tokens, output_tokens, reasoning_tokens, cost_usd, time.monotonic() - started,
    )


def _sse_error(code: int) -> bytes:
    """An error the engine's OpenAI client will recognise mid-stream. Carries
    a status and nothing else — an upstream error body can echo the prompt."""
    payload = json.dumps({"error": {"code": code, "message": "upstream error"}})
    return f"data: {payload}\n\ndata: [DONE]\n\n".encode()


# ── concurrency primitive ────────────────────────────────────────────────
# Keyed by running event loop, not a module-level singleton. An
# asyncio.Semaphore binds to the loop it was created on; a single shared one
# would be correct in production (one loop for the process's life) and
# quietly broken under pytest-asyncio, which gives each test its own loop and
# would raise "bound to a different event loop" on the second test to run.
_semaphores: "dict[int, asyncio.Semaphore]" = {}


def _call_semaphore() -> "asyncio.Semaphore":
    loop = asyncio.get_running_loop()
    existing = _semaphores.get(id(loop))
    if existing is None:
        existing = asyncio.Semaphore(_MAX_CONCURRENT_CALLS)
        _semaphores[id(loop)] = existing
    return existing


async def _relay(upstream, usage: dict):
    """Copy one upstream response to the engine: an SSE error event for a non-200, else the stream verbatim.

    Usage, which arrives in the final chunk when include_usage is set, is written into `usage` on the way past.
    """
    if upstream.status_code != 200:
        await upstream.aread()
        outcome = "rate_limited" if upstream.status_code == 429 else "upstream_error"
        _gateway_calls.inc(outcome=outcome)
        logger.warning(
            "presentation gateway: upstream %s (stream)", upstream.status_code
        )
        # The stream has already begun as far as the client is
        # concerned, so the status cannot be changed now. An
        # SSE error event is what an OpenAI-compatible client
        # understands, and the engine surfaces it as a failure.
        yield _sse_error(upstream.status_code)
        return

    async for line in upstream.aiter_lines():
        if line.startswith("data: "):
            chunk = line[6:].strip()
            if chunk and chunk != "[DONE]":
                try:
                    parsed = json.loads(chunk)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("usage"):
                    usage.update(parsed["usage"])
        # aiter_lines strips the newlines that frame an SSE
        # event; both must go back or the client sees one
        # unterminated event and blocks.
        yield (line + "\n\n").encode() if line else b"\n"
