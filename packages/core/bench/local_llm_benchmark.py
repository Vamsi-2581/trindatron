"""Local LLM (llama.cpp, llm.local) vs the cheapest API in the catalog, on cheap everyday tasks. Not imported by src/.

    uv run --no-sync python bench/local_llm_benchmark.py            # local + API (API capped at US$0.01)
    uv run --no-sync python bench/local_llm_benchmark.py --local-only

Tasks are deliberately simple (classification, short summary, structured extraction,
text transformation, a one-line answer in Portuguese) and scored deterministically, so
a number here means "did the exact thing asked", not "sounded plausible". No advanced
reasoning: this measures whether a local model can take the cheapest work, nothing more.

The API side calls the catalog model directly through litellm (not the router: this is a
measurement, not user traffic), prices it from PROVIDER_CATALOG, and stops before the
running cost could pass the cap. A missing credential reports BLOCKED and calls nothing.

Local compute is not free. It is priced at LOCAL_CPU_USD_PER_HOUR — an ASSUMPTION for a
4 vCPU / 16 GB on-demand VM that could host this runtime — times the measured seconds.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

API_KEY = "groq-llama4-scout"  # catalog alias; its model_id is groq/openai/gpt-oss-20b (cheapest tier)
API_CAP_USD = 0.01
LOCAL_BASE = "http://127.0.0.1:8181/v1"
LOCAL_MODEL = "qwen2.5-0.5b-instruct-q4_k_m"
LOCAL_CPU_USD_PER_HOUR = 0.17  # assumption, see module docstring
MAX_TOKENS = 200
RESULTS = Path(__file__).resolve().parent / "results" / "local_llm_benchmark.json"


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", text).strip()


def _json(text: str):
    match = re.search(r"\{.*\}|\[.*\]", text or "", re.S)
    try:
        return json.loads(match.group(0)) if match else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Task:
    key: str
    kind: str
    prompt: str
    check: object  # callable(answer) -> float in [0, 1]


def _label(expected: str, options: tuple[str, ...]):
    def score(answer: str) -> float:
        found = [o for o in options if re.search(rf"\b{o}\b", _norm(answer))]
        return 1.0 if found == [expected] else 0.0
    return score


def _summary(required: tuple[str, ...], max_words: int):
    def score(answer: str) -> float:
        words = len((answer or "").split())
        hits = sum(1 for r in required if r in _norm(answer))
        return (hits / len(required)) * (1.0 if 0 < words <= max_words else 0.5)
    return score


def _fields(expected: dict):
    def score(answer: str) -> float:
        data = _json(answer)
        if not isinstance(data, dict):
            return 0.0
        return sum(1 for k, v in expected.items() if _norm(str(data.get(k, ""))) == _norm(str(v))) / len(expected)
    return score


def _exact(expected: str):
    return lambda answer: 1.0 if _norm(answer).strip(" .\"'`") == _norm(expected) else 0.0


def _array(expected: list[str]):
    def score(answer: str) -> float:
        data = _json(answer)
        return 1.0 if isinstance(data, list) and [_norm(str(x)) for x in data] == [_norm(x) for x in expected] else 0.0
    return score


SENT = ("positivo", "negativo", "neutro")
INTENT = ("cancelar", "suporte", "compra")
TASKS: tuple[Task, ...] = (
    Task("cls-sentiment-1", "classificação",
         f"Classifique o sentimento da frase com UMA palavra ({', '.join(SENT)}). Frase: \"Adorei o atendimento, "
         "resolveram tudo em minutos.\" Responda só a palavra.", _label("positivo", SENT)),
    Task("cls-sentiment-2", "classificação",
         f"Classifique o sentimento da frase com UMA palavra ({', '.join(SENT)}). Frase: \"O produto chegou "
         "quebrado e ninguém responde.\" Responda só a palavra.", _label("negativo", SENT)),
    Task("cls-intent", "classificação",
         f"Qual a intenção do cliente? Responda com UMA palavra entre: {', '.join(INTENT)}. Mensagem: \"Quero "
         "encerrar minha assinatura hoje.\"", _label("cancelar", INTENT)),
    Task("sum-1", "resumo curto",
         "Resuma em no máximo 20 palavras: \"A prefeitura anunciou que a ponte principal ficará fechada de segunda "
         "a sexta para reparos na estrutura. O trânsito será desviado pela avenida central e ônibus terão novas "
         "rotas durante a obra.\"", _summary(("ponte", "fech", "reparo"), 20)),
    Task("sum-2", "resumo curto",
         "Resuma em no máximo 15 palavras: \"A empresa registrou lucro recorde no trimestre, impulsionada pelas "
         "vendas online, e vai contratar 200 pessoas até dezembro.\"", _summary(("lucro", "contrat"), 15)),
    Task("ext-1", "extração estruturada",
         "Extraia em JSON com as chaves nome, cidade e idade (idade como número): \"Meu nome é Ana Souza, tenho 34 "
         "anos e moro em Curitiba.\" Responda só o JSON.", _fields({"nome": "Ana Souza", "cidade": "Curitiba",
                                                                     "idade": "34"})),
    Task("ext-2", "extração estruturada",
         "Extraia em JSON com as chaves produto e quantidade (número): \"Preciso de 12 cadeiras de escritório "
         "para a sala nova.\" Responda só o JSON.", _fields({"produto": "cadeiras de escritório", "quantidade": "12"})),
    Task("tr-date", "transformação",
         "Converta a data 18/09/2026 para o formato ISO AAAA-MM-DD. Responda só a data.", _exact("2026-09-18")),
    Task("tr-list", "transformação",
         "Transforme em um array JSON de strings, na mesma ordem: \"maçã, banana e laranja\". Responda só o JSON.",
         _array(["maçã", "banana", "laranja"])),
    Task("qa-capital", "resposta simples",
         "Qual é a capital do Brasil? Responda só o nome da cidade.", _exact("Brasília")),
)


@dataclass
class Outcome:
    task: str
    kind: str
    runner: str
    score: float
    latency_s: float
    prompt_tokens: int
    completion_tokens: int
    tokens_per_second: float | None
    cost_usd: float
    error: str = ""
    answer: str = ""


async def run_local() -> list[Outcome]:
    from src.local_runtime.llm import LocalLLMClient, LocalRuntimeUnavailable

    client = LocalLLMClient(LOCAL_BASE, LOCAL_MODEL, timeout_s=60)
    if not await client.health():
        return [Outcome(t.key, t.kind, "local", 0.0, 0.0, 0, 0, None, 0.0, error="BLOCKED: runtime not running")
                for t in TASKS]
    out = []
    for task in TASKS:
        try:
            c = await client.chat([{"role": "user", "content": task.prompt}], max_tokens=MAX_TOKENS, temperature=0)
        except LocalRuntimeUnavailable as exc:
            out.append(Outcome(task.key, task.kind, "local", 0.0, 0.0, 0, 0, None, 0.0, error=exc.reason))
            continue
        cost = c.seconds / 3600 * LOCAL_CPU_USD_PER_HOUR
        out.append(Outcome(task.key, task.kind, "local", float(task.check(c.text)), c.seconds, c.prompt_tokens,
                           c.completion_tokens, c.tokens_per_second, cost, answer=c.text[:160]))
    return out


async def run_api() -> list[Outcome]:
    import litellm
    from trinity_router.providers import PROVIDER_CATALOG

    cfg = PROVIDER_CATALOG[API_KEY]
    if not os.environ.get("GROQ_API_KEY"):
        from src.config import settings
        if settings.groq_api_key:
            os.environ["GROQ_API_KEY"] = settings.groq_api_key  # this process only, never printed
    if not os.environ.get("GROQ_API_KEY"):
        return [Outcome(t.key, t.kind, "api", 0.0, 0.0, 0, 0, None, 0.0, error="BLOCKED: no credential") for t in TASKS]
    out, spent = [], 0.0
    # Worst case per call: prompt ~150 tokens, completion capped at MAX_TOKENS (reasoning counts as output).
    worst = (400 * cfg.cost_per_1m_input_usd + (MAX_TOKENS + 800) * cfg.cost_per_1m_output_usd) / 1e6
    for task in TASKS:
        if spent + worst > API_CAP_USD:
            out.append(Outcome(task.key, task.kind, "api", 0.0, 0.0, 0, 0, None, 0.0, error="SKIPPED: cost cap"))
            continue
        started = time.monotonic()
        try:
            r = await litellm.acompletion(model=cfg.model_id, messages=[{"role": "user", "content": task.prompt}],
                                          max_tokens=MAX_TOKENS + 800, temperature=0)
        except Exception as exc:
            out.append(Outcome(task.key, task.kind, "api", 0.0, time.monotonic() - started, 0, 0, None, 0.0,
                               error=type(exc).__name__))
            continue
        seconds = time.monotonic() - started
        usage = getattr(r, "usage", None)
        pin, pout = int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(usage, "completion_tokens", 0) or 0)
        cost = (pin * cfg.cost_per_1m_input_usd + pout * cfg.cost_per_1m_output_usd) / 1e6
        spent += cost
        text = r.choices[0].message.content or ""
        out.append(Outcome(task.key, task.kind, "api", float(task.check(text)), seconds, pin, pout,
                           pout / seconds if seconds else None, cost, answer=text[:160]))
    return out


def summarize(outcomes: list[Outcome]) -> dict:
    rows = {}
    for o in outcomes:
        row = rows.setdefault((o.kind, o.runner), {"n": 0, "score": 0.0, "latency": 0.0, "cost": 0.0, "errors": 0})
        row["n"] += 1
        row["score"] += o.score
        row["latency"] += o.latency_s
        row["cost"] += o.cost_usd
        row["errors"] += 1 if o.error else 0
    return {f"{kind}|{runner}": {"quality": round(v["score"] / v["n"], 3), "latency_s": round(v["latency"] / v["n"], 3),
                                 "cost_usd_per_task": round(v["cost"] / v["n"], 8), "error_rate": v["errors"] / v["n"],
                                 "n": v["n"]} for (kind, runner), v in rows.items()}


async def main() -> None:
    local = await run_local()
    api = [] if "--local-only" in sys.argv else await run_api()
    outcomes = local + api
    report = {"generated": time.strftime("%Y-%m-%dT%H:%M:%S"), "local_model": LOCAL_MODEL, "api_model": API_KEY,
              "local_cpu_usd_per_hour_assumption": LOCAL_CPU_USD_PER_HOUR, "api_cap_usd": API_CAP_USD,
              "api_spent_usd": round(sum(o.cost_usd for o in api), 8),
              "summary": summarize(outcomes), "outcomes": [asdict(o) for o in outcomes]}
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for key, row in sorted(report["summary"].items()):
        print(f"{key:32} quality={row['quality']:.2f} latency={row['latency_s']:.2f}s "
              f"cost/task=${row['cost_usd_per_task']:.7f} errors={row['error_rate']:.0%}")
    print("api spent: $%.6f (cap $%.2f)" % (report["api_spent_usd"], API_CAP_USD))


if __name__ == "__main__":
    asyncio.run(main())
