"""Free Compute Pool: free allowances used first, never depended on (free_pool.py, router.py)."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from trinity_router import free_pool
from trinity_router import router as router_module
from trinity_router.free_pool import FreePoolTracker, _parse_duration, prefix_for
from trinity_router.providers import PROVIDER_CATALOG
from trinity_router.router import ModelRouter, ProviderUnavailableError, RoutingRequest
from trinity_router.selector import TaskType

GROQ_FREE, CEREBRAS_FREE, CLOUDFLARE_FREE = (
    "free-groq-gpt-oss-120b", "free-cerebras-gpt-oss-120b", "free-cloudflare-gpt-oss-120b",
)


@pytest.fixture
def all_sources(monkeypatch):
    for name in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.setenv(name, "test")
    monkeypatch.delenv("FREE_POOL_ENABLED", raising=False)


@pytest.fixture
def no_sources(monkeypatch):
    for name in ("GROQ_API_KEY", "CEREBRAS_API_KEY", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID"):
        monkeypatch.delenv(name, raising=False)


def _chain(*keys):
    return [(key, PROVIDER_CATALOG[key]) for key in keys]


def _text(content="Olá, tudo bem?"):
    return [{"role": "user", "content": content}]


# ── Which free sources go in front, and how they are billed ──────────────────

def test_free_sources_stand_in_front_of_a_chain_they_are_at_least_as_good_as(all_sources):
    prefix = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                        input_tokens_estimate=2_000, messages=_text())
    assert [key for key, _ in prefix] == [GROQ_FREE, CEREBRAS_FREE, CLOUDFLARE_FREE]


def test_a_free_call_is_billed_as_the_model_it_stood_in_for(all_sources):
    primary = PROVIDER_CATALOG["gemini-25-flash-lite"]
    [(_, billed), *_] = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                                   input_tokens_estimate=2_000, messages=_text())
    assert billed.cost_per_1m_input_usd == primary.cost_per_1m_input_usd
    assert billed.cost_per_1m_output_usd == primary.cost_per_1m_output_usd
    assert billed.model_id == "groq/openai/gpt-oss-120b"


@pytest.mark.parametrize("primary", ["claude-sonnet", "claude-opus", "gpt-4o"])
def test_a_free_source_never_replaces_a_better_model(all_sources, primary):
    assert prefix_for(_chain(primary), required_quality_class=None, needs_tools=False,
                      input_tokens_estimate=2_000, messages=_text()) == []


def test_a_request_needing_a_better_model_gets_none(all_sources):
    assert prefix_for(_chain("groq-llama4-scout"), required_quality_class="standard", needs_tools=False,
                      input_tokens_estimate=2_000, messages=_text()) == []


def test_tools_long_context_and_attachments_go_to_the_sources_that_can_take_them(all_sources):
    with_tools = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=True,
                            input_tokens_estimate=2_000, messages=_text())
    assert CLOUDFLARE_FREE not in [key for key, _ in with_tools]  # no tool calling there
    long = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                      input_tokens_estimate=20_000, messages=_text())
    assert CEREBRAS_FREE not in [key for key, _ in long]  # the free tier holds context to 8,192
    image = [{"role": "user", "content": [{"type": "text", "text": "o que é isto?"},
                                          {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}}]}]
    assert prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                      input_tokens_estimate=2_000, messages=image) == []


def test_only_sources_with_credentials_and_the_pool_on(monkeypatch, no_sources):
    monkeypatch.setenv("GROQ_API_KEY", "test")
    assert [key for key, _ in prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None,
                                         needs_tools=False, input_tokens_estimate=2_000,
                                         messages=_text())] == [GROQ_FREE]
    monkeypatch.setenv("FREE_POOL_ENABLED", "false")
    assert prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                      input_tokens_estimate=2_000, messages=_text()) == []


def test_an_operator_disabled_source_is_left_out(all_sources):
    prefix = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=False,
                        input_tokens_estimate=2_000, messages=_text(), disabled_keys={GROQ_FREE})
    assert GROQ_FREE not in [key for key, _ in prefix]


def test_no_source_is_one_the_data_would_train_on():
    providers = {item.model.provider for item in free_pool.FREE_SOURCES}
    assert providers == {"groq", "cerebras", "cloudflare"}
    assert not any("gemini" in item.model.model_id or ":free" in item.model.model_id
                   for item in free_pool.FREE_SOURCES)


# ── Quota tracking ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


NOON = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc).timestamp()


def test_a_daily_request_allowance_is_used_up_to_its_safety_margin():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    for _ in range(int(1_000 * free_pool.SAFETY_MARGIN)):
        clock.now += 3  # well inside the per-minute allowance
        assert tracker.can_use(GROQ_FREE, tokens_estimate=10) is None
        tracker.record_success(GROQ_FREE, tokens=10)
    clock.now += 3
    assert tracker.can_use(GROQ_FREE, tokens_estimate=10) == "quota"


def test_the_per_minute_token_allowance_frees_up_as_the_minute_passes():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    tracker.record_success(GROQ_FREE, tokens=6_000)
    assert tracker.can_use(GROQ_FREE, tokens_estimate=3_000) == "quota"  # 8,000 a minute, 90% usable
    clock.now += 61
    assert tracker.can_use(GROQ_FREE, tokens_estimate=3_000) is None


def test_a_dollar_allowance_counts_list_price_spend():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    tracker.record_success(CLOUDFLARE_FREE, tokens=100_000, usd=0.095)
    assert tracker.can_use(CLOUDFLARE_FREE, tokens_estimate=1_000, usd_estimate=0.01) == "quota"
    clock.now = datetime(2026, 9, 28, 0, 0, 1, tzinfo=timezone.utc).timestamp()  # the next UTC day
    assert tracker.can_use(CLOUDFLARE_FREE, tokens_estimate=1_000, usd_estimate=0.01) is None


def test_a_429_cools_a_source_down_for_as_long_as_the_provider_says():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    assert tracker.record_rate_limited(GROQ_FREE, retry_after_s=12.0) == 12.0
    assert tracker.can_use(GROQ_FREE, tokens_estimate=10) == "cooldown"
    clock.now += 13
    assert tracker.can_use(GROQ_FREE, tokens_estimate=10) is None


def test_without_a_retry_after_a_busy_minute_waits_a_minute_and_a_spent_day_until_midnight():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    assert tracker.record_rate_limited(GROQ_FREE) == 60.0
    assert tracker.record_rate_limited(CLOUDFLARE_FREE, daily=True) == pytest.approx(12 * 3600)


def test_the_providers_own_count_wins_when_it_says_the_day_is_spent():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    tracker.record_success(GROQ_FREE, tokens=10, headers={"x-ratelimit-remaining-requests": "0",
                                                          "x-ratelimit-reset-requests": "1m26.4s"})
    assert tracker.can_use(GROQ_FREE, tokens_estimate=10) == "cooldown"
    clock.now += 87
    assert tracker.can_use(GROQ_FREE, tokens_estimate=10) is None


@pytest.mark.parametrize("text, seconds", [("1m26.4s", 86.4), ("592ms", 0.592), ("7.66s", 7.66), ("30", 30.0),
                                           ("1h2m", 3720.0), (None, None), ("", None)])
def test_reset_durations_in_the_providers_format(text, seconds):
    assert _parse_duration(text) == (pytest.approx(seconds) if seconds is not None else None)


# ── Through the router ────────────────────────────────────────────────────────

def _response(content="Resposta", prompt_tokens=200, completion_tokens=100):
    message = MagicMock(); message.content = content; message.tool_calls = None
    usage = MagicMock(); usage.prompt_tokens = prompt_tokens; usage.completion_tokens = completion_tokens
    usage.prompt_tokens_details = None
    choice = MagicMock(); choice.message = message
    response = MagicMock(); response.choices = [choice]; response.usage = usage
    response._hidden_params = {"additional_headers": {"llm_provider-x-ratelimit-remaining-requests": "990"}}
    return response


class RateLimited(Exception):
    status_code = 429

    def __init__(self, retry_after: str | None = None) -> None:
        super().__init__("Rate limit reached for model")
        self.response = MagicMock()
        self.response.headers = {"retry-after": retry_after} if retry_after else {}


def _request(plan="free", **overrides):
    values = dict(messages=_text(), task_type=TaskType.CHAT, user_plan=plan, session_id="s1")
    values.update(overrides)
    return RoutingRequest(**values)


def _litellm(monkeypatch, behaviour):
    calls: list[str] = []

    async def acompletion(*args, **kwargs):
        calls.append(kwargs["model"])
        outcome = behaviour(kwargs["model"], len(calls))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion)
    return calls


def _first_paid(router, request):
    decision = router._build_chain_decision(
        request.task_type, request.user_plan, router_module._effective_limits(request), request.capability,
        forced_provider_key=None, disabled_provider_keys=frozenset(), messages=request.messages, tools=None,
        requested_max_output_tokens=None,
    )
    return decision.chain[0]


async def test_the_free_plan_is_served_by_a_free_source_first(monkeypatch, all_sources):
    calls = _litellm(monkeypatch, lambda model, n: _response())
    router = ModelRouter()
    result = await router.complete(_request("free"))
    assert calls == ["groq/openai/gpt-oss-120b"]
    _, primary = _first_paid(router, _request("free"))  # the model the router would have used
    billed = (200 * primary.cost_per_1m_input_usd + 100 * primary.cost_per_1m_output_usd) / 1_000_000
    assert result.cost_usd == pytest.approx(billed)  # the plan pays what it always paid


async def test_a_429_moves_on_cools_the_source_and_leaves_its_breaker_alone(monkeypatch, all_sources):
    def behaviour(model, n):
        return RateLimited("30") if model == "groq/openai/gpt-oss-120b" else _response()

    calls = _litellm(monkeypatch, behaviour)
    router = ModelRouter(sleep_fn=lambda s: _no_sleep())
    await router.complete(_request("free"))
    assert calls == ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"]
    assert router._circuit_breakers.is_available(GROQ_FREE)

    calls.clear()
    await router.complete(_request("free"))
    assert calls == ["cerebras/gpt-oss-120b"]  # still cooling: skipped without a call


async def _no_sleep():
    return None


async def test_when_every_free_source_is_out_the_paid_chain_answers(monkeypatch, all_sources):
    def behaviour(model, n):
        return RateLimited() if model.startswith(("groq/openai/gpt-oss-120b", "cerebras/", "openai/@cf/")) \
            else _response()

    calls = _litellm(monkeypatch, behaviour)
    router = ModelRouter()
    result = await router.complete(_request("free"))
    assert calls[-1] == _first_paid(router, _request("free"))[1].model_id
    assert result.content == "Resposta"


async def test_a_free_source_that_rejects_the_request_never_reaches_the_person(monkeypatch, all_sources):
    class BadRequest(Exception):
        status_code = 400

    def behaviour(model, n):
        return BadRequest("unsupported parameter") if model.startswith(("groq/openai/gpt-oss-120b", "cerebras/",
                                                                        "openai/@cf/")) else _response()

    calls = _litellm(monkeypatch, behaviour)
    router = ModelRouter()
    result = await router.complete(_request("free"))
    assert result.content == "Resposta" and calls[-1] == _first_paid(router, _request("free"))[1].model_id


async def test_all_sources_down_and_no_paid_route_left_is_an_ordinary_outage(monkeypatch, all_sources):
    _litellm(monkeypatch, lambda model, n: RateLimited())
    with pytest.raises(ProviderUnavailableError):
        await ModelRouter(sleep_fn=lambda s: _no_sleep()).complete(_request("free"))


@pytest.mark.parametrize("primary", ["claude-sonnet", "claude-opus"])
def test_when_the_router_chose_a_better_model_no_free_source_is_put_in_front(all_sources, primary):
    decision = MagicMock(chain=_chain(primary), complexity=None)
    chain = ModelRouter()._with_free_prefix(decision, _request("max"), 2_000)
    assert [key for key, _ in chain] == [primary]


def test_when_the_router_chose_a_simple_model_for_a_simple_request_free_goes_first(all_sources):
    # PRO and MAX too: for a short, simple request the router itself picks a low-class model (complexity.py);
    # a free model at least as good then answers it at no cost, billed as that model.
    decision = MagicMock(chain=_chain("mistral-small", "claude-sonnet"), complexity=None)
    chain = ModelRouter()._with_free_prefix(decision, _request("pro"), 2_000)
    assert [key for key, _ in chain][:3] == [GROQ_FREE, CEREBRAS_FREE, CLOUDFLARE_FREE]
    assert [key for key, _ in chain][3:] == ["mistral-small", "claude-sonnet"]


async def test_a_forced_model_is_never_preceded_by_a_free_source(monkeypatch, all_sources):
    calls = _litellm(monkeypatch, lambda model, n: _response())
    await ModelRouter().complete(_request("free", forced_provider_key="gemini-25-flash-lite"))
    assert calls == [PROVIDER_CATALOG["gemini-25-flash-lite"].model_id]


async def test_starts_groq_primary_on_the_free_tier_cools_down_on_429_instead_of_tripping(monkeypatch,
                                                                                            no_sources):
    def behaviour(model, n):
        return RateLimited("45") if model == PROVIDER_CATALOG["groq-llama4-scout"].model_id else _response()

    calls = _litellm(monkeypatch, behaviour)
    router = ModelRouter(sleep_fn=lambda s: _no_sleep())
    for _ in range(4):
        await router.complete(_request("start"))
    assert calls.count(PROVIDER_CATALOG["groq-llama4-scout"].model_id) == 1  # then skipped while cooling
    assert router._circuit_breakers.is_available("groq-llama4-scout")


async def test_streaming_takes_the_same_free_first_path(monkeypatch, all_sources):
    class Chunk:
        def __init__(self, text=None, usage=None):
            delta = MagicMock(); delta.content = text
            self.choices = [MagicMock(delta=delta)] if text else []
            self.usage = usage

    async def stream_of(text):
        usage = MagicMock(prompt_tokens=50, completion_tokens=20, prompt_tokens_details=None)
        for piece in (text, None):
            yield Chunk(piece) if piece else Chunk(usage=usage)

    calls: list[str] = []

    async def acompletion(*args, **kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "groq/openai/gpt-oss-120b":
            raise RateLimited("20")
        return stream_of("Uma resposta longa o bastante para ser liberada ao usuário." * 3)

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion)
    chunks = [chunk async for chunk in ModelRouter(sleep_fn=lambda s: _no_sleep()).stream(_request("free"))]
    assert calls[:2] == ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"]
    assert "".join(chunk.text or "" for chunk in chunks).startswith("Uma resposta")


# ── Simple work only: precision, research and critical analysis go to the chosen model ──

from trinity_router.free_pool import precision_reason  # noqa: E402

PRECISE = [
    # The request whose answer a free model got wrong twice in production testing (2026-09-27).
    ("Em uma frase: o que é o Lago do Manso?", "factual"),
    ("onde fica Chapada dos Guimarães", "factual"),
    ("quem foi Santos Dumont", "factual"),
    ("em que ano caiu o muro de Berlim", "factual"),
    ("quanto custa o dólar hoje", "factual"),
    ("qual é a capital da Austrália", "factual"),
    ("What is the Treaty of Tordesillas?", "factual"),
    ("how many people live in Cuiabá", "factual"),
    ("¿dónde queda Machu Picchu?", "factual"),
    ("qual a dosagem de dipirona para criança", "exactness"),
    ("me explique a lei do inquilinato", "exactness"),
    ("cite fontes sobre isso", "exactness"),
    ("is it true that vaccines cause autism", "exactness"),
    ("analise este plano de negócios", "analysis"),
    ("compare Python e Go para uma API", "analysis"),
    ("por que o céu é azul?", "analysis"),
]
SIMPLE = [
    "Oi, tudo bem?",
    "Escreva uma mensagem gentil para remarcar uma reunião",
    "Traduza para o espanhol: bom dia",
    "Organize minha semana com três prazos",
    "o que é amor?",
    "me dê ideias de nomes para uma padaria",
    "resuma este texto em 3 linhas: a reunião foi adiada para sexta.",
]


@pytest.mark.parametrize("text, reason", PRECISE)
def test_a_request_that_needs_exact_facts_or_analysis_is_recognised(text, reason):
    assert precision_reason(_text(text), "chat") == reason


@pytest.mark.parametrize("text", SIMPLE)
def test_simple_work_stays_with_the_free_pool(text):
    assert precision_reason(_text(text), "chat") is None


@pytest.mark.parametrize("capability", ["research", "files"])
def test_research_and_reading_documents_never_use_a_free_model(capability):
    assert precision_reason(_text("Oi"), capability) == "research"


def test_only_the_persons_latest_message_decides():
    messages = [{"role": "user", "content": "quem foi Santos Dumont?"},
                {"role": "assistant", "content": "Um inventor brasileiro."},
                {"role": "user", "content": "Obrigado! Escreva um parabéns para minha mãe."}]
    assert precision_reason(messages, "chat") is None


@pytest.mark.parametrize("text, _", PRECISE)
def test_the_router_puts_no_free_source_in_front_of_a_precise_request(all_sources, text, _):
    decision = MagicMock(chain=_chain("groq-llama4-scout", "gemini-25-flash-lite"), complexity=None)
    chain = ModelRouter()._with_free_prefix(decision, _request("free", messages=_text(text)), 2_000)
    assert [key for key, _ in chain] == ["groq-llama4-scout", "gemini-25-flash-lite"]


@pytest.mark.parametrize("text", SIMPLE)
def test_the_router_keeps_the_free_pool_in_front_of_simple_work(all_sources, text):
    decision = MagicMock(chain=_chain("groq-llama4-scout", "gemini-25-flash-lite"), complexity=None)
    chain = ModelRouter()._with_free_prefix(decision, _request("free", messages=_text(text)), 2_000)
    assert [key for key, _ in chain][:3] == [GROQ_FREE, CEREBRAS_FREE, CLOUDFLARE_FREE]


async def test_a_factual_question_is_answered_by_the_paid_model_end_to_end(monkeypatch, all_sources):
    calls = _litellm(monkeypatch, lambda model, n: _response())
    router = ModelRouter()
    request = _request("free", messages=_text("Em uma frase: o que é o Lago do Manso?"))
    await router.complete(request)
    assert calls == [_first_paid(router, request)[1].model_id]
    assert not any(model in {s.model.model_id for s in free_pool.FREE_SOURCES} for model in calls)


# ── Cloudflare Workers AI: the third free source ─────────────────────────────

def test_cloudflare_is_the_third_free_source_after_groq_and_cerebras(all_sources):
    assert [item.key for item in free_pool.configured_sources()] == [GROQ_FREE, CEREBRAS_FREE, CLOUDFLARE_FREE]


def test_cloudflare_is_reached_on_its_official_openai_compatible_endpoint(monkeypatch, all_sources):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    cloudflare = free_pool.source(CLOUDFLARE_FREE)
    assert cloudflare.model.model_id == "openai/@cf/openai/gpt-oss-120b"
    assert free_pool.call_kwargs(CLOUDFLARE_FREE) == {
        "api_base": "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1", "api_key": "cf-token"}
    assert free_pool.call_kwargs(GROQ_FREE) == {} and free_pool.call_kwargs("claude-sonnet") == {}


def test_cloudflare_follows_the_official_quota():
    allowance = free_pool.source(CLOUDFLARE_FREE).allowance
    assert allowance.usd_per_day == pytest.approx(10_000 * 0.011 / 1_000)  # 10,000 Neurons a day
    assert allowance.requests_per_minute == 300


def test_cloudflare_without_its_account_id_or_token_is_absent(monkeypatch, all_sources):
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID")
    assert CLOUDFLARE_FREE not in [item.key for item in free_pool.configured_sources()]
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN")
    assert CLOUDFLARE_FREE not in [item.key for item in free_pool.configured_sources()]


def test_the_days_neurons_spent_keeps_cloudflare_out_until_midnight_utc():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    spent = Exception("4006: you have used up your daily free allocation of 10,000 neurons")
    assert free_pool.is_rate_limit(spent) and free_pool.is_daily_exhaustion(spent, CLOUDFLARE_FREE)
    wait = tracker.record_rate_limited(CLOUDFLARE_FREE, daily=free_pool.is_daily_exhaustion(spent, CLOUDFLARE_FREE))
    assert wait == pytest.approx(12 * 3600)  # not the one minute a busy minute would get
    clock.now += 11 * 3600
    assert tracker.can_use(CLOUDFLARE_FREE, tokens_estimate=10) == "cooldown"
    clock.now = datetime(2026, 9, 28, 0, 0, 1, tzinfo=timezone.utc).timestamp()
    assert tracker.can_use(CLOUDFLARE_FREE, tokens_estimate=10) is None


def test_a_busy_minute_on_cloudflare_is_only_a_minute():
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    busy = RateLimited()
    assert not free_pool.is_daily_exhaustion(busy, CLOUDFLARE_FREE)
    assert tracker.record_rate_limited(CLOUDFLARE_FREE, daily=free_pool.is_daily_exhaustion(busy, CLOUDFLARE_FREE)) == 60.0


async def test_when_groq_and_cerebras_are_out_cloudflare_answers_on_its_own_endpoint(monkeypatch, all_sources):
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "acc123")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "cf-token")
    seen: list[dict] = []

    async def acompletion(*args, **kwargs):
        seen.append(kwargs)
        if kwargs["model"] in ("groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b"):
            raise RateLimited()
        return _response()

    monkeypatch.setattr("trinity_router.router.litellm.acompletion", acompletion)
    router = ModelRouter(sleep_fn=lambda s: _no_sleep())
    result = await router.complete(_request("free"))
    last = seen[-1]
    assert [call["model"] for call in seen] == ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b",
                                               "openai/@cf/openai/gpt-oss-120b"]
    assert last["api_base"] == "https://api.cloudflare.com/client/v4/accounts/acc123/ai/v1"
    assert last["api_key"] == "cf-token"
    assert "api_base" not in seen[0] and "api_key" not in seen[0]  # other sources keep their own settings
    assert result.content == "Resposta"
    assert router._free_pool.snapshot()[CLOUDFLARE_FREE]["requests_today"] == 1


async def test_when_cloudflare_is_out_too_the_paid_chain_answers(monkeypatch, all_sources):
    calls = _litellm(monkeypatch, lambda model, n: Exception("daily free allocation of 10,000 neurons used")
                     if model.startswith("openai/@cf/") else RateLimited()
                     if model in ("groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b") else _response())
    router = ModelRouter(sleep_fn=lambda s: _no_sleep())
    result = await router.complete(_request("free"))
    assert calls[:3] == ["groq/openai/gpt-oss-120b", "cerebras/gpt-oss-120b", "openai/@cf/openai/gpt-oss-120b"]
    assert calls[-1] == _first_paid(router, _request("free"))[1].model_id and result.content == "Resposta"
    assert router._circuit_breakers.is_available(CLOUDFLARE_FREE)  # a spent day is not a broken provider


def test_cloudflare_never_takes_a_request_that_needs_tools(all_sources):
    prefix = prefix_for(_chain("gemini-25-flash-lite"), required_quality_class=None, needs_tools=True,
                        input_tokens_estimate=2_000, messages=_text())
    assert CLOUDFLARE_FREE not in [key for key, _ in prefix]


# ── "The day is spent" is Cloudflare's answer only (review of PR #18) ────────

class Status(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def test_a_groq_daily_limit_without_retry_after_is_not_a_day_long_pause():
    # START's model: Groq's daily limits are a rolling 24 h window. Without a retry-after it cools down like any
    # rate limit (a minute), instead of sending START's traffic to a paid model until 00:00 UTC.
    clock = Clock(NOON)
    tracker = FreePoolTracker(clock=clock)
    tpd = Status("Rate limit reached for model openai/gpt-oss-20b on tokens per day (TPD)", 429)
    for key in ("groq-llama4-scout", GROQ_FREE, CEREBRAS_FREE):
        assert free_pool.is_rate_limit(tpd) and not free_pool.is_daily_exhaustion(tpd, key)
    assert tracker.record_rate_limited("groq-llama4-scout",
                                       daily=free_pool.is_daily_exhaustion(tpd, "groq-llama4-scout")) == 60.0


def test_a_word_like_allocation_in_a_provider_error_is_not_a_quota():
    oom = Status("CUDA error: memory allocation failed", 500)
    assert not free_pool.is_rate_limit(oom)
    assert not free_pool.is_daily_exhaustion(oom, CLOUDFLARE_FREE)


@pytest.mark.parametrize("error, daily", [
    (Status("you have used up your daily free allocation of 10,000 neurons", 429), True),
    (Status("4006: daily free allocation of Neurons exceeded"), True),          # names the Neurons: enough
    (Status("Your account has exceeded its daily allocation", 429), True),      # a real 429 about the day
    (Status("daily allocation exceeded", 500), False),                          # not a 429, no Neurons
    (Status("Too many requests", 429), False),                                   # a busy minute
])
def test_cloudflare_is_out_for_the_day_only_on_its_own_explicit_answer(error, daily):
    assert free_pool.is_daily_exhaustion(error, CLOUDFLARE_FREE) is daily


async def test_through_the_router_a_groq_daily_429_pauses_it_for_a_minute_not_the_day(monkeypatch, no_sources):
    calls = _litellm(monkeypatch, lambda model, n: Status("Rate limit reached ... tokens per day (TPD)", 429)
                     if model == PROVIDER_CATALOG["groq-llama4-scout"].model_id else _response())
    router = ModelRouter(sleep_fn=lambda s: _no_sleep())
    await router.complete(_request("start"))
    cooling = router._free_pool.snapshot()["groq-llama4-scout"]["cooling_s"]
    assert 0 < cooling <= 60
    assert router._circuit_breakers.is_available("groq-llama4-scout")


# ── "What is the latest…" is a fact about today ────────────────────────────────
# Production, 2026-09-28: "qual foi o ultimo modelo de carro eletrico criado no brasil" reached the free pool and
# was answered with a July 2025 launch as "the latest" in September 2026.
RECENT = [
    "qual foi o ultimo modelo de carro eletrico criado no brasil",
    "qual é o celular mais recente da Samsung?",
    "quem é o atual presidente do Banco Central?",
    "what is the latest iPhone?",
    "which is the newest Tesla model this year",
    "¿cuál es el modelo más reciente de BYD?",
]


@pytest.mark.parametrize("text", RECENT)
def test_a_question_about_what_is_newest_never_uses_a_free_model(text):
    assert precision_reason(_text(text), "chat") in {"recency", "factual"}


def test_the_production_question_is_recognised_as_recency():
    assert precision_reason(_text("qual foi o ultimo modelo de carro eletrico criado no brasil"), "chat") == "recency"


@pytest.mark.parametrize("text", ["me ajude a atualizar meu currículo", "atualmente estou cansado, me anime"])
def test_recency_needs_the_whole_word(text):
    """'atualizar' is not 'atual'; a mood is not a fact to look up — though 'atualmente' does count, on purpose."""
    reason = precision_reason(_text(text), "chat")
    assert reason is None if "atualizar" in text else reason == "recency"
