"""Free Compute Pool — free inference quota used first, never depended on.

Some providers give a real, recurring free allowance to an account, through the same API as their paid
service, under terms that allow it and without using the data for training (checked 2026-09-27):

  groq        free tier, per model: 30 req/min, 1,000 req/day, 8,000 tokens/min, 200,000 tokens/day
              (TRINDATRON's account confirmed on this tier from its own x-ratelimit headers). Groq does not
              retain inference data by default and does not train on it.
  cerebras    free tier: 1,000,000 tokens/day, ~5 req/min, context held to 8,192 tokens. Inputs and outputs
              are not stored, logged or reused.
  cloudflare  Workers AI: 10,000 Neurons/day on every account, reset 00:00 UTC (10,000 x US$ 0.011 / 1,000 =
              US$ 0.11 of compute), 300 text-generation requests/min; customer content is not used to train
              models (developers.cloudflare.com/workers-ai: pricing, limits, data usage — 2026-09-27). Called on
              Cloudflare's OpenAI-compatible endpoint (/accounts/{id}/ai/v1), the one its docs give for gpt-oss.

Deliberately NOT here: Gemini's free tier and Mistral's free "Experiment" tier (both use the data for training
by default), OpenRouter ":free" models (logging depends on the host behind them; 50 req/day), GitHub Models and
NVIDIA's API catalog (their terms limit them to prototyping). Several accounts to multiply a quota are never used.

How it is used (router.py):

  * A free model is put IN FRONT of a request's chain only when it is at least as good as what the chain
    would have used (quality class, tools, context, plain text input). It never replaces a better model.
  * Before each call the tracker checks the local count against the provider's published allowance (with a
    safety margin) and any cooldown; an exhausted or cooling source is skipped at no cost.
  * A 429 or a quota answer from a free source is not a provider failure: the source cools down until its
    reset (retry-after, or the next window) and the chain moves on. The circuit breaker is left alone.
  * After a free source the chain continues with the paid route it always had. No request ever depends on a
    free source, and a free source never counts against the plan's failover attempts.
  * A free answer is billed to the person's plan exactly as the model it stood in for would have been: plans
    and prices do not change; the saving is the company's.

State is in memory, per router instance (one per process), like the circuit breaker. Several processes each
keep their own count; a source they overdraw together answers 429, which is handled the same way.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from .metrics import router_metrics
from .providers import QUALITY_CLASS_ORDER, ProviderModel, _TEXT_CAPABILITIES, _TEXT_TASK_TYPES

logger = logging.getLogger("trinity.router.free_pool")

_free_calls = router_metrics.counter(
    "router_free_pool_total",
    "Free pool decisions per source: served, skipped_quota, skipped_cooldown, rate_limited, failed.",
)

#: Use this share of a published allowance: the provider's own count and ours never line up exactly.
SAFETY_MARGIN = 0.9


@dataclass(frozen=True)
class FreeAllowance:
    """A source's published free allowance. None means the provider publishes no such limit."""

    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    tokens_per_day: int | None = None
    #: A daily allowance in US dollars of compute (Cloudflare's Neurons), spent at the model's list price.
    usd_per_day: float | None = None


@dataclass(frozen=True)
class FreeSource:
    key: str
    model: ProviderModel
    allowance: FreeAllowance
    #: Environment variables litellm needs for this provider; a source without them is simply absent.
    env: tuple[str, ...]
    #: Extra litellm arguments for a source reached on its own endpoint (base URL and key, read from `env`
    #: at call time). None for providers litellm reaches by name alone.
    endpoint: Callable[[], dict] | None = None


def _cloudflare_endpoint() -> dict:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    return {"api_base": f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/v1",
            "api_key": os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()}


def call_kwargs(key: str) -> dict:
    """What the router adds to litellm's call for this source (nothing for providers reached by name)."""
    found = source(key)
    return found.endpoint() if found is not None and found.endpoint is not None else {}


def _model(provider: str, model_id: str, *, context_window: int, cost_in: float, cost_out: float,
           supports_tools: bool, priority: int) -> ProviderModel:
    return ProviderModel(
        provider=provider, model_id=model_id, context_window=context_window,
        cost_per_1m_input_usd=cost_in, cost_per_1m_output_usd=cost_out,
        supports_tools=supports_tools, priority=priority, quality_class="low", speed_class="fast",
        reasoning_level="medium", coding_level="medium", supports_agent=supports_tools,
        supports_reasoning=True, supported_task_types=_TEXT_TASK_TYPES,
        supported_capabilities=_TEXT_CAPABILITIES,
    )


#: gpt-oss-120b is the model all three sources serve free: OpenAI's open-weight (Apache-2.0) 120B reasoning
#: model — a step above gpt-4o-mini/mistral-small for reasoning and code, served on fast hardware.
#: Costs are each provider's paid list price; they only bound a request's budget check (the call is free).
FREE_SOURCES: tuple[FreeSource, ...] = (
    FreeSource(
        key="free-groq-gpt-oss-120b",
        model=_model("groq", "groq/openai/gpt-oss-120b", context_window=131_072, cost_in=0.15, cost_out=0.60,
                     supports_tools=True, priority=1),
        allowance=FreeAllowance(requests_per_minute=30, requests_per_day=1_000, tokens_per_minute=8_000,
                                tokens_per_day=200_000),
        env=("GROQ_API_KEY",),
    ),
    FreeSource(
        key="free-cerebras-gpt-oss-120b",
        # The free tier holds context to 8,192 tokens: longer requests go straight past it.
        model=_model("cerebras", "cerebras/gpt-oss-120b", context_window=8_192, cost_in=0.35, cost_out=0.75,
                     supports_tools=True, priority=2),
        allowance=FreeAllowance(requests_per_minute=5, tokens_per_minute=30_000, tokens_per_day=1_000_000),
        env=("CEREBRAS_API_KEY",),
    ),
    FreeSource(
        key="free-cloudflare-gpt-oss-120b",
        # The same model as the two sources above, so an answer does not change with the source that gave it.
        # Its official price (US$ 0.35 / 0.75 per 1M tokens) is what the day's Neurons are counted in. Tools are
        # left to Groq and Cerebras: Cloudflare lists function calling for this model, but it is not verified
        # on this endpoint yet, and a free source must never be the reason a tool call breaks.
        model=_model("cloudflare", "openai/@cf/openai/gpt-oss-120b", context_window=128_000, cost_in=0.35,
                     cost_out=0.75, supports_tools=False, priority=3),
        # 10,000 Neurons x US$ 0.011 / 1,000 = US$ 0.11 a day, shared by every Workers AI model on the account;
        # 300 text-generation requests a minute.
        allowance=FreeAllowance(requests_per_minute=300, usd_per_day=0.11),
        env=("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"),
        endpoint=_cloudflare_endpoint,
    ),
)

#: Cloudflare's allowance resets once a day at 00:00 UTC: when it says the day's Neurons are spent, it stays out
#: until then, not for a minute. Only Cloudflare's own answer counts — Groq's daily limits are a rolling 24 h
#: window with a retry-after, and a word like "daily" or "allocation" in another provider's error means nothing.
_NEURONS = re.compile(r"\bneurons?\b", re.IGNORECASE)
_DAILY_WORDS = re.compile(r"daily|per\s+day|allocation", re.IGNORECASE)

#: Models already in a paid chain that run on a free allowance on TRINDATRON's account. They stay where the
#: chain puts them; the tracker only keeps them from being hammered past their quota and keeps a 429 from
#: opening their circuit breaker. groq-llama4-scout is gpt-oss-20b on the same free Groq tier (confirmed).
TRACKED_CHAIN_MODELS: dict[str, FreeAllowance] = {
    "groq-llama4-scout": FreeAllowance(requests_per_minute=30, requests_per_day=1_000, tokens_per_minute=8_000,
                                       tokens_per_day=200_000),
}


def enabled() -> bool:
    return os.environ.get("FREE_POOL_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def configured_sources() -> list[FreeSource]:
    """The sources this deployment can call: the pool on, and each source's credentials present."""
    if not enabled():
        return []
    return [source for source in FREE_SOURCES if all(os.environ.get(name) for name in source.env)]


def source(key: str) -> FreeSource | None:
    return next((item for item in FREE_SOURCES if item.key == key), None)


def is_free(key: str) -> bool:
    return source(key) is not None or key in TRACKED_CHAIN_MODELS


def allowance_for(key: str) -> FreeAllowance | None:
    found = source(key)
    return found.allowance if found is not None else TRACKED_CHAIN_MODELS.get(key)


def _rank(quality_class: str) -> int:
    return QUALITY_CLASS_ORDER.index(quality_class) if quality_class in QUALITY_CLASS_ORDER else 0


# ── Where a free model must not answer ────────────────────────────────────────
# The free pool is for simple work. A request that needs the facts exactly right, research, or critical
# analysis goes to the model the router chose for it: a free model got the location of Lago do Manso wrong
# twice in the production test of this pool (2026-09-27), and a cheap wrong fact costs more than a paid one.

#: Capabilities whose whole point is getting facts right: web research and reading the person's documents.
PRECISION_CAPABILITIES = frozenset({"research", "files"})

_FACTUAL_QUESTION = re.compile(
    r"\b(?:"
    # PT-BR
    r"quem\s+(?:é|e|foi|s[ãa]o|eram|inventou|descobriu|escreveu|fundou|ganhou)|onde\s+(?:fica|é|foi|est[áa]|nasceu)|"
    r"quando\s+(?:foi|é|aconteceu|come[çc]ou|terminou|nasceu|morreu)|em\s+que\s+ano|"
    r"quant[oa]s?\s+(?:é|s[ãa]o|custa|custou|habitantes|pessoas|anos|vale)|"
    r"qual\s+(?:é|foi)\s+(?:o|a)\s+(?:capital|popula[çc][ãa]o|pre[çc]o|data|valor|taxa|al[íi]quota|lei|altura|"
    r"dist[âa]ncia|[áa]rea|cota[çc][ãa]o|resultado|placar)|"
    # EN
    r"who\s+(?:is|was|are|were|invented|discovered|wrote|founded|won)|where\s+(?:is|was|are|did)|"
    r"when\s+(?:was|is|did|does)|what\s+year|how\s+(?:many|much)|"
    r"what\s+(?:is|was)\s+the\s+(?:capital|population|price|date|rate|law|height|distance|area|score)|"
    # ES
    r"qui[ée]n\s+(?:es|fue|son|invent[óo]|descubri[óo]|escribi[óo])|d[óo]nde\s+(?:est[áa]|queda|es|naci[óo])|"
    r"cu[áa]ndo\s+(?:fue|es|ocurri[óo])|en\s+qu[ée]\s+a[ñn]o|cu[áa]nt[oa]s?\s+(?:es|son|cuesta|habitantes)"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)
# "O que é o Lago do Manso?", "What is the Treaty of Tordesillas?": a definition of something named.
_NAMED_THING = re.compile(
    r"\b(?i:o\s+que\s+(?:é|e|foi|s[ãa]o)|what\s+(?:is|was|are)|qu[ée]\s+(?:es|fue|son))\s+"
    r"(?:(?i:o|a|os|as|the|el|la|los|las)\s+)?[A-ZÁÉÍÓÚÂÊÔÃÕÇ]",
    re.UNICODE,
)
# "Qual foi o último carro elétrico lançado?": the answer is whatever is newest today, so it is a fact about the
# present that a free model gets wrong by answering from memory (seen 2026-09-28: a July 2025 launch given as
# "the latest" in September 2026).
_RECENCY = re.compile(
    r"\b(?:"
    r"[úu]ltim[oa]s?|mais\s+recentes?|mais\s+nov[oa]s?|atual|atuais|atualmente|hoje\s+em\s+dia|neste\s+ano|este\s+ano|"
    r"latest|newest|most\s+recent|current(?:ly)?|nowadays|this\s+year|"
    r"m[áa]s\s+recientes?|actual(?:es|mente)?"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)
_EXACTNESS_DOMAIN = re.compile(
    r"\b(?:"
    r"lei|leis|legisla[çc][ãa]o|jur[íi]dic[oa]|jurisprud[êe]ncia|artigo\s+\d|cl[áa]usula|tribut\w*|impost[oa]s?|"
    r"diagn[óo]stic\w*|sintoma\w*|medicament\w*|rem[ée]dio\w*|dosagem|dose|bula|m[ée]dic[oa]|"
    r"investiment\w*|juros|cota[çc][ãa]o|a[çc][õo]es\s+da|infla[çc][ãa]o|estat[íi]stic\w*|"
    r"fonte|fontes|cite|cita[çc][ãa]o|refer[êe]ncias?|verifique|confirme|fact.?check|[ée]\s+verdade|"
    r"law|legal|lawsuit|statute|tax|taxes|medical|diagnos\w+|symptoms?|dosage|medication|drug|"
    r"investment|interest\s+rate|stock\s+price|statistics?|sources?|citations?|cite|verify|is\s+it\s+true|"
    r"ley|leyes|legal|impuestos?|m[ée]dico|dosis|medicamento|inversi[óo]n|estad[íi]stic\w*|fuentes?|verifica"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def _last_user_text(messages: list[dict] | None) -> str:
    for message in reversed(messages or ()):
        if isinstance(message, dict) and message.get("role") == "user" and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def precision_reason(messages: list[dict] | None, capability: str | None) -> str | None:
    """Why this request must not be answered by a free model, or None when a simple answer is fine.

    Only what the router can tell from the request itself: its capability, and the person's own words
    (a factual question, a field where accuracy is the point, or a demand for critical analysis — the same
    reasoning language the complexity engine already reads).
    """
    from .complexity import _REASONING_PATTERN

    if (capability or "").lower() in PRECISION_CAPABILITIES:
        return "research"
    text = _last_user_text(messages)
    if not text:
        return None
    if _FACTUAL_QUESTION.search(text) or _NAMED_THING.search(text):
        return "factual"
    if _EXACTNESS_DOMAIN.search(text):
        return "exactness"
    if _RECENCY.search(text):
        return "recency"
    if _REASONING_PATTERN.search(text):
        return "analysis"
    return None


def _plain_text(messages: list[dict] | None) -> bool:
    """Only text: an image or a file part goes to the models the chain chose for it."""
    return all(isinstance(message.get("content"), str) or message.get("content") is None
               for message in messages or () if isinstance(message, dict))


def prefix_for(chain: list[tuple[str, ProviderModel]], *, required_quality_class: str | None, needs_tools: bool,
               input_tokens_estimate: int, messages: list[dict] | None,
               disabled_keys: frozenset[str] | set[str] = frozenset()) -> list[tuple[str, ProviderModel]]:
    """The free sources to try before `chain`, each billed as the chain's first model would have been.

    Empty unless every free source kept is at least as good as that first model and can serve this request.
    """
    if not chain or not _plain_text(messages):
        return []
    primary_key, primary = chain[0]
    floor = max(_rank(primary.quality_class), _rank(required_quality_class or "ultra_low"))
    prefix = []
    for item in configured_sources():
        model = item.model
        if item.key in disabled_keys or _rank(model.quality_class) < floor:
            continue
        if needs_tools and not model.supports_tools:
            continue
        if input_tokens_estimate > model.context_window:
            continue
        # Billed as the model it stands in for: plans and prices stay as they are.
        billed = replace(model, cost_per_1m_input_usd=primary.cost_per_1m_input_usd,
                         cost_per_1m_output_usd=primary.cost_per_1m_output_usd,
                         cost_per_1m_cached_input_usd=primary.cost_per_1m_cached_input_usd)
        prefix.append((item.key, billed))
    return prefix


# ── Quota tracking ────────────────────────────────────────────────────────────


def _utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def _seconds_to_utc_midnight(now: float) -> float:
    moment = datetime.fromtimestamp(now, tz=timezone.utc)
    tomorrow = (moment + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1.0, (tomorrow - moment).total_seconds())


def _parse_duration(value: str | None) -> float | None:
    """Groq's reset format ("1m26.4s", "592ms", "7.66s") or plain seconds, in seconds."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    total, number = 0.0, ""
    index = 0
    while index < len(text):
        char = text[index]
        if char.isdigit() or char == ".":
            number += char
            index += 1
            continue
        unit = "ms" if text[index:index + 2] == "ms" else char
        index += len(unit)
        if not number:
            return None
        amount = float(number)
        number = ""
        total += {"h": 3600, "m": 60, "s": 1, "ms": 0.001}.get(unit, 0) * amount
    return total or None


@dataclass
class _Usage:
    day: str = ""
    requests_today: int = 0
    tokens_today: int = 0
    usd_today: float = 0.0
    minute: list = field(default_factory=list)  # (timestamp, tokens) of the last 60 s
    cooldown_until: float = 0.0


class FreePoolTracker:
    """How much of each free allowance this process has used, and which sources are cooling down."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._usage: dict[str, _Usage] = {}
        self._lock = threading.Lock()

    def _get(self, key: str, now: float) -> _Usage:
        usage = self._usage.setdefault(key, _Usage())
        today = _utc_day(now)
        if usage.day != today:
            usage.day, usage.requests_today, usage.tokens_today, usage.usd_today = today, 0, 0, 0.0
        usage.minute = [(at, tokens) for at, tokens in usage.minute if now - at < 60]
        return usage

    def cooling_down(self, key: str) -> str | None:
        """"cooldown" while the provider's own answer (a 429, or its count at zero) holds the source off."""
        now = self._clock()
        with self._lock:
            return "cooldown" if now < self._get(key, now).cooldown_until else None

    def can_use(self, key: str, *, tokens_estimate: int, usd_estimate: float = 0.0) -> str | None:
        """None when the source may take this request; otherwise why not ("cooldown" or "quota")."""
        allowance = allowance_for(key)
        if allowance is None:
            return None
        now = self._clock()
        with self._lock:
            usage = self._get(key, now)
            if now < usage.cooldown_until:
                return "cooldown"
            margin = SAFETY_MARGIN
            minute_tokens = sum(tokens for _, tokens in usage.minute)
            checks = (
                (allowance.requests_per_minute, len(usage.minute) + 1),
                (allowance.requests_per_day, usage.requests_today + 1),
                (allowance.tokens_per_minute, minute_tokens + tokens_estimate),
                (allowance.tokens_per_day, usage.tokens_today + tokens_estimate),
            )
            if any(limit is not None and wanted > limit * margin for limit, wanted in checks):
                return "quota"
            if allowance.usd_per_day is not None and usage.usd_today + usd_estimate > allowance.usd_per_day * margin:
                return "quota"
            return None

    def record_success(self, key: str, *, tokens: int, usd: float = 0.0, headers: dict | None = None) -> None:
        now = self._clock()
        with self._lock:
            usage = self._get(key, now)
            usage.requests_today += 1
            usage.tokens_today += max(0, tokens)
            usage.usd_today += max(0.0, usd)
            usage.minute.append((now, max(0, tokens)))
            # The provider's own count wins when it says it is nearly out (another process, or our estimate).
            remaining = _header(headers, "x-ratelimit-remaining-requests")
            if remaining is not None and remaining <= 0:
                reset = _parse_duration(_header_text(headers, "x-ratelimit-reset-requests"))
                usage.cooldown_until = now + (reset or _seconds_to_utc_midnight(now))

    def record_rate_limited(self, key: str, *, retry_after_s: float | None = None, daily: bool = False) -> float:
        """A 429 or a quota answer: cool down until the provider says, else a minute, else the next day.

        `daily`: the answer said the day's allowance is spent (Cloudflare's Neurons) — out until 00:00 UTC.
        """
        now = self._clock()
        with self._lock:
            usage = self._get(key, now)
            allowance = allowance_for(key) or FreeAllowance()
            if retry_after_s:
                wait = retry_after_s
            elif daily:
                wait = _seconds_to_utc_midnight(now)
            elif allowance.tokens_per_minute or allowance.requests_per_minute:
                wait = 60.0
            else:
                wait = _seconds_to_utc_midnight(now)
            usage.cooldown_until = max(usage.cooldown_until, now + wait)
            return wait

    def snapshot(self) -> dict[str, dict]:
        now = self._clock()
        with self._lock:
            return {key: {"requests_today": usage.requests_today, "tokens_today": usage.tokens_today,
                          "cooling_s": max(0.0, round(usage.cooldown_until - now, 1))}
                    for key, usage in ((key, self._get(key, now)) for key in list(self._usage))}


def _header_text(headers: dict | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        lowered = str(key).lower()
        if lowered == name or lowered.endswith("-" + name) or lowered.endswith(name):
            return str(value)
    return None


def _header(headers: dict | None, name: str) -> float | None:
    text = _header_text(headers, name)
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def is_rate_limit(exc: Exception) -> bool:
    """A 429 or an exhausted-quota answer, whatever the provider's wording."""
    status = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return ("ratelimit" in name or "rate limit" in text or "quota" in text or "too many requests" in text
            or bool(_NEURONS.search(text)))


def _status(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)


def is_daily_exhaustion(exc: Exception, key: str) -> bool:
    """Cloudflare said the day's Neurons are spent — not a busy minute, and never another provider's answer.

    Either the answer names the Neurons, or it is a real 429 that speaks of the day's allocation.
    """
    found = source(key)
    if found is None or found.model.provider != "cloudflare":
        return False
    text = str(exc)
    return bool(_NEURONS.search(text)) or (_status(exc) == 429 and bool(_DAILY_WORDS.search(text)))


def retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    return _parse_duration(_header_text(dict(headers) if headers else None, "retry-after"))


def count(outcome: str, key: str) -> None:
    _free_calls.inc(outcome=outcome, source=key)
