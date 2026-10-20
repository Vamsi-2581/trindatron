"""The two sides of the Presenton deployment, generated from code instead of typed into a dashboard.

    uv run python -m src.presentation.deploy_env

prints the environment of the Presenton service and the variables the Core needs to reach it. Secrets appear
only as placeholders — this never reads or prints a real one — so the output can be pasted and reviewed safely,
and the secrets are then filled in Railway's own variable store (infra/presenton/README.md §5).

Why it exists: the documented gateway address was `core.railway.internal:8000`, while this project's Core is the
Railway service TRINITY-AI-DEV (private domain `trinity-ai-dev.railway.internal`) listening on Railway's default
port 8080. A hand-copied address would have failed the first call of every deck. The addresses live here once,
and the tests pin them.
"""
from __future__ import annotations

from src.config import settings
from src.presentation.presenton import engine_environment
from src.presentation.provider import ImageStrategy

#: This project's Core on Railway's private network: service TRINITY-AI-DEV, Railway's default $PORT.
CORE_PRIVATE_URL = "http://trinity-ai-dev.railway.internal:8080"
#: The Presenton service on the same private network (its container listens on 80 — README §3).
PRESENTON_PRIVATE_URL = "http://presenton.railway.internal"
#: Where Core serves the engine's LLM calls (src/api/routes/presenton_gateway.py; /chat/completions under it).
GATEWAY_PATH = "/internal/presenton/llm/v1"

GATEWAY_TOKEN_PLACEHOLDER = "<PRESENTON_LLM_GATEWAY_TOKEN: same random value (>= 32 chars) on both services>"
PRESENTON_API_KEY_PLACEHOLDER = "<PRESENTON_API_KEY: sk-presenton-..., minted by the engine's admin endpoint>"
PEXELS_PLACEHOLDER = "<PEXELS_API_KEY: optional, free tier - leave unset for no stock photos>"


def presenton_service_environment(*, core_url: str = CORE_PRIVATE_URL) -> dict[str, str]:
    """The Presenton container's environment: stock-first images, the model pinned, Gemini only via Core."""
    env = engine_environment(
        strategy=ImageStrategy.STOCK_FIRST,
        llm_base_url=core_url.rstrip("/") + GATEWAY_PATH,
        llm_gateway_token=GATEWAY_TOKEN_PLACEHOLDER,
        model=settings.presentation_model,
        pexels_api_key=PEXELS_PLACEHOLDER,
    )
    return env


def core_variables(*, presenton_url: str = PRESENTON_PRIVATE_URL) -> dict[str, str]:
    """What the Core service adds to reach the engine."""
    return {
        "PRESENTON_BASE_URL": presenton_url,
        "PRESENTON_API_KEY": PRESENTON_API_KEY_PLACEHOLDER,
        "PRESENTON_LLM_GATEWAY_TOKEN": GATEWAY_TOKEN_PLACEHOLDER,
    }


def render() -> str:
    def block(title: str, values: dict[str, str]) -> str:
        return "\n".join([f"# {title}", *(f"{key}={value}" for key, value in values.items())])

    return "\n\n".join([
        block("Railway service `presenton` (image ghcr.io/presenton/presenton:latest, port 80, volume /app_data)",
              presenton_service_environment()),
        block("Railway service `TRINITY-AI-DEV` (Core) - add these", core_variables()),
    ]) + "\n"


if __name__ == "__main__":  # pragma: no cover - a command, exercised through render()
    print(render(), end="")
