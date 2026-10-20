"""Etapa 5G (Observability) — redaction and correlation-ID log plumbing.

Covers: redact() never leaves a recognizable credential pattern in the
output; JsonFormatter applies it to both the message and any formatted
exception traceback; RequestIdLogFilter stamps the current contextvar
value (or None) onto every LogRecord.
"""
from __future__ import annotations

import json
import logging

from src.middleware.logging import JsonFormatter, RequestIdLogFilter
from src.observability.context import bind_request_id, get_request_id, reset_request_id
from src.observability.redaction import redact


# ═════════════════════════════════════════════════════════════════════════
# redact()
# ═════════════════════════════════════════════════════════════════════════

def test_redacts_bearer_token():
    out = redact("Authorization: Bearer abc123.def456-XYZ")
    assert "abc123.def456-XYZ" not in out
    assert "Bearer [REDACTED]" in out


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyMSJ9.4Adcj3UFYzPUVaVF43FmMab6RlaQD8A9V8wFzzht-KQ"
    out = redact(f"token={jwt}")
    assert jwt not in out
    assert "[REDACTED_JWT]" in out


def test_redacts_anthropic_style_key():
    out = redact("using key sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in out
    assert "[REDACTED_API_KEY]" in out


def test_redacts_openai_style_key():
    out = redact("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz1234567890")
    assert "[REDACTED_API_KEY]" in out


def test_redacts_google_api_key():
    out = redact("key=AIzaSyD-abcdefghijklmnopqrstuvwxyz0123456")
    assert "[REDACTED_API_KEY]" in out


def test_redacts_groq_api_key():
    out = redact("gsk_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "[REDACTED_API_KEY]" in out


def test_leaves_ordinary_log_message_untouched():
    msg = "entitlement reserved | user=abc123 session=def456 plan=start tcu_reserved=10.0000"
    assert redact(msg) == msg


def test_leaves_uuid_looking_ids_untouched():
    """A UUID must never be mistaken for a credential — session/execution
    ids are load-bearing for correlating logs across this codebase."""
    msg = "continuity debited | execution_id=3fa85f64-5717-4562-b3fc-2c963f66afa6"
    assert redact(msg) == msg


def test_empty_string_is_a_no_op():
    assert redact("") == ""


# ═════════════════════════════════════════════════════════════════════════
# JsonFormatter — applies redact() to message and exception text
# ═════════════════════════════════════════════════════════════════════════

def _make_record(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="trinity.test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=exc_info,
    )


def test_json_formatter_redacts_message():
    record = _make_record("leaked Bearer abcdef123456")
    out = json.loads(JsonFormatter().format(record))
    assert "abcdef123456" not in out["message"]
    assert out["message"].startswith("leaked Bearer [REDACTED]")


def test_json_formatter_redacts_exception_text():
    try:
        raise ValueError("failed with key sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    except ValueError:
        import sys
        record = _make_record("boom", exc_info=sys.exc_info())

    out = json.loads(JsonFormatter().format(record))
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz" not in out["exception"]
    assert "[REDACTED_API_KEY]" in out["exception"]


def test_json_formatter_omits_request_id_when_absent():
    record = _make_record("no request in flight")
    out = json.loads(JsonFormatter().format(record))
    assert "request_id" not in out


def test_json_formatter_includes_request_id_when_present():
    record = _make_record("inside a request")
    record.request_id = "req-abc-123"
    out = json.loads(JsonFormatter().format(record))
    assert out["request_id"] == "req-abc-123"


# ═════════════════════════════════════════════════════════════════════════
# RequestIdLogFilter + context propagation
# ═════════════════════════════════════════════════════════════════════════

def test_filter_stamps_none_outside_any_request():
    assert get_request_id() is None
    record = _make_record("outside a request")
    RequestIdLogFilter().filter(record)
    assert record.request_id is None


def test_filter_stamps_bound_request_id():
    token = bind_request_id("req-xyz-789")
    try:
        record = _make_record("inside a request")
        RequestIdLogFilter().filter(record)
        assert record.request_id == "req-xyz-789"
    finally:
        reset_request_id(token)
    assert get_request_id() is None
