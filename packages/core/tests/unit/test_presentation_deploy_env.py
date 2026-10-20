"""The Presenton deployment, generated from code: the right addresses, the load-bearing settings, no real secret.

The documented gateway address was `core.railway.internal:8000`; this project's Core is `trinity-ai-dev` on
Railway's port 8080. A deck's first call would have failed on the hand-copied value.
"""
from __future__ import annotations

from src.api.routes.presenton_gateway import router as gateway_router
from src.config import settings
from src.presentation import deploy_env


def test_the_engine_calls_this_projects_core_gateway_on_the_private_network():
    env = deploy_env.presenton_service_environment()
    assert env["CUSTOM_LLM_URL"] == "http://trinity-ai-dev.railway.internal:8080/internal/presenton/llm/v1"
    assert "core.railway.internal" not in deploy_env.render() and ":8000" not in deploy_env.render()


def test_the_gateway_path_is_the_one_core_actually_serves():
    served = {route.path for route in gateway_router.routes}
    assert deploy_env.GATEWAY_PATH + "/chat/completions" in served


def test_the_core_reaches_the_engine_on_the_private_network():
    assert deploy_env.core_variables()["PRESENTON_BASE_URL"] == "http://presenton.railway.internal"


def test_the_load_bearing_engine_settings_are_there():
    env = deploy_env.presenton_service_environment()
    assert env["LLM"] == "custom" and env["CUSTOM_MODEL"] == settings.presentation_model
    assert env["CAN_CHANGE_KEYS"] == "false"
    assert env["ENABLE_PARALLEL_IMAGE_GENERATION"] == "false"  # 0/6 in parallel vs 5/6 in sequence (README §4)
    assert env["IMAGE_PROVIDER"] == "pexels" and env["DISABLE_IMAGE_GENERATION"] == "true"
    assert "GOOGLE_API_KEY" not in env and "GOOGLE_MODEL" not in env  # the Gemini key never reaches the engine


def test_secrets_are_placeholders_never_values(monkeypatch):
    """Even with real secrets in the Core's settings, the rendered config carries only placeholders."""
    monkeypatch.setattr(settings, "presenton_llm_gateway_token", "real-gateway-token-0123456789abcdef")
    monkeypatch.setattr(settings, "presenton_api_key", "sk-presenton-realvalue")
    monkeypatch.setattr(settings, "gemini_api_key", "AIza-real-gemini-key")
    text = deploy_env.render()
    for secret in ("real-gateway-token-0123456789abcdef", "sk-presenton-realvalue", "AIza-real-gemini-key"):
        assert secret not in text
    env, core = deploy_env.presenton_service_environment(), deploy_env.core_variables()
    assert env["CUSTOM_LLM_API_KEY"] == core["PRESENTON_LLM_GATEWAY_TOKEN"] == deploy_env.GATEWAY_TOKEN_PLACEHOLDER
    assert core["PRESENTON_API_KEY"].startswith("<") and env["PEXELS_API_KEY"].startswith("<")


def test_the_rendered_config_is_plain_ascii_key_value_lines():
    text = deploy_env.render()
    assert text.isascii()
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines and all("=" in line and " " not in line.split("=", 1)[0] for line in lines)
