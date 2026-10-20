"""Log redaction — Etapa 5G (Observability), defense-in-depth.

Every log call site already audited for this etapa (chat_service.py,
agent_service.py, entitlement_service.py, continuity_*.py, request_control.py,
agent_scheduler.py, trinity_router's audit.py) logs only metadata — user
ids, session ids, model/provider names, token counts, cost/TCU, durations,
rejection reasons — and NEVER raw message content, prompts, or credentials.
audit.py's own module docstring makes this an explicit, tested invariant
for the Model Router side (`_sanitize_error()` strips raw provider
exception text down to a coarse category before it ever reaches a log
line).

This module is a SECOND, independent layer on top of that discipline, not
a replacement for it: `redact()` runs over the fully-formatted log message
right before it leaves the process (JsonFormatter, src/middleware/logging.py)
and scrubs anything that LOOKS LIKE a credential, so a future log call
site that accidentally interpolates a secret (a bug, not an intended
design) still doesn't reach stdout/log aggregators in the clear.

Patterns are deliberately narrow (known key formats, Bearer/JWT tokens) —
broad "redact anything long and random" heuristics would also eat
legitimate UUIDs, session ids, and execution ids that logs and audits
throughout this codebase rely on being readable.
"""
from __future__ import annotations

import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Authorization: Bearer <token>
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9\-_\.=]+"), "Bearer [REDACTED]"),
    # JSON Web Tokens (header.payload.signature, base64url segments)
    (re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_JWT]"),
    # Anthropic ("sk-ant-...") / OpenAI ("sk-...") style secret keys
    (re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9\-_]{10,}"), "[REDACTED_API_KEY]"),
    # Google API keys
    (re.compile(r"\bAIza[0-9A-Za-z\-_]{20,}"), "[REDACTED_API_KEY]"),
    # Groq API keys
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), "[REDACTED_API_KEY]"),
    # Slack-style tokens (unlikely today, cheap to cover)
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]+"), "[REDACTED_TOKEN]"),
]


def redact(text: str) -> str:
    """Return `text` with any recognizable credential pattern replaced by
    a fixed placeholder. Safe to call on every log line — a no-op (no
    regex matches) is the overwhelmingly common case."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text
