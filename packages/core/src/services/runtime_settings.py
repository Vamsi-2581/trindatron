"""Runtime Settings — Etapa 5H (Kill Switches + Runtime Settings).

The ONE server-side mechanism for operational controls that must be
changeable WITHOUT a redeploy: Agent enabled/disabled, Continuity Mode
enabled/disabled, per-provider/model disabled, per-capability disabled.
Before this etapa, every piece of Core's configuration was env-var-only
(src/config.py's pydantic Settings, read once at process start — see the
5H audit) — changing anything required editing the environment and
restarting the process. This module is deliberately the SMALLEST
mechanism that closes that gap: one key-value table
(src/models/runtime_setting.py, one row per known key), a small fixed set
of KNOWN_KEYS (never "dozens of settings just to have a runtime-config
system" — only the switches Etapa 5H actually needs), and typed accessor
functions every other call site uses instead of touching the table
directly.

Does NOT create: a second authentication system (admin authorization
reuses get_current_user()'s existing JWT mechanism — see
src/api/deps.py::get_current_admin_user()), a second feature-flag/config
service, Redis/Kafka, or any new financial/entitlement semantics.

── Read model: always fresh, never cached ──────────────────────────────
Every accessor below queries the database directly, every time — no
in-process cache, no TTL. This is deliberate, not an oversight: caching
would reintroduce exactly the "can a race condition let a request bypass
a switch flipped mid-flight" risk Etapa 5H explicitly asks to rule out.
A single-row PK lookup on a table capped at 4 rows (KNOWN_KEYS's size) is
cheap enough that the correctness this buys is unambiguously worth it —
see test_runtime_settings.py's concurrency proof (a write followed
immediately by a read on a DIFFERENT db session always observes the new
value).

── Fail-safe defaults (see FAIL-SAFE section of the Etapa 5H spec) ─────
Two DIFFERENT scenarios, two DIFFERENT correct defaults:

  1. SETTING INEXISTENTE (no row for that key at all — the normal state
     for every key until an admin ever touches it): resolves to
     `_DEFAULT_WHEN_MISSING`, which is exactly the CURRENT/pre-5H
     behavior — Agent and Continuity enabled, nothing disabled. Absence
     of configuration must never change existing behavior.

  2. VALOR CORROMPIDO (a row exists — someone/something set it — but its
     value fails validation, e.g. a manual DB edit, a truncated write, or
     a future admin-route bug that stored the wrong type): resolves to
     `_DEFAULT_WHEN_CORRUPTED`, which is DELIBERATELY NOT always the same
     as `_DEFAULT_WHEN_MISSING`:
       - agent_enabled / continuity_enabled (critical boolean kill
         switches): corrupted -> False (fail CLOSED). The whole point of
         a kill switch is "an operator can turn this off when something
         is wrong." If its stored value becomes unreadable, silently
         treating that as "enabled" would mean a corrupted database
         could UN-DO an operator's emergency shutoff at the worst
         possible moment — the literal "habilitação insegura" the Etapa
         5H spec calls out. Failing closed costs nothing when the row
         was never meant to be there in the first place (that's case 1,
         handled separately, correctly, as "enabled") and only matters
         when someone genuinely tried to set a value and it got mangled.
       - disabled_provider_keys / disabled_capabilities (enumerations of
         things an operator turned OFF): corrupted -> `[]` (empty), i.e.
         the SAME as the missing-row default. A corrupted enumeration
         has no natural "fail closed" reading that isn't actively worse
         than doing nothing (interpreting corruption as "disable
         everything" would manufacture a self-inflicted full outage from
         a data bug, which nobody asked for and is a strictly worse
         failure mode than reverting to the already-approved, fully-
         tested Etapa 5E baseline of "every catalog provider available").

Both branches log at ERROR level (never silently swallowed) so a
corrupted row is visible in ops without ever crashing the request that
happened to read it.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session as DBSession

from src.models.runtime_setting import RuntimeSetting
from src.observability.metrics import core_metrics

logger = logging.getLogger("trinity.runtime_settings")

_settings_changes_total = core_metrics.counter(
    "runtime_settings_changes_total", "Admin-initiated runtime setting changes, labeled by key.",
)

# ── Known keys ────────────────────────────────────────────────────────────
AGENT_ENABLED = "agent_enabled"
CONTINUITY_ENABLED = "continuity_enabled"
DISABLED_PROVIDER_KEYS = "disabled_provider_keys"
DISABLED_CAPABILITIES = "disabled_capabilities"
# How many images a FREE account may create per local day. Adjustable without
# a deploy because the right number follows measured engine cost (media/plans.py).
FREE_IMAGE_DAILY_LIMIT = "free_image_daily_limit"

_BOOL_KEYS = frozenset({AGENT_ENABLED, CONTINUITY_ENABLED})
_STRING_LIST_KEYS = frozenset({DISABLED_PROVIDER_KEYS, DISABLED_CAPABILITIES})
_INT_KEYS = frozenset({FREE_IMAGE_DAILY_LIMIT})
_INT_BOUNDS = {FREE_IMAGE_DAILY_LIMIT: (0, 100)}
KNOWN_KEYS = _BOOL_KEYS | _STRING_LIST_KEYS | _INT_KEYS

_DEFAULT_WHEN_MISSING: dict[str, Any] = {
    AGENT_ENABLED: True,
    CONTINUITY_ENABLED: True,
    DISABLED_PROVIDER_KEYS: [],
    DISABLED_CAPABILITIES: [],
    # None = use the plan table's own figure.
    FREE_IMAGE_DAILY_LIMIT: None,
}

_DEFAULT_WHEN_CORRUPTED: dict[str, Any] = {
    AGENT_ENABLED: False,
    CONTINUITY_ENABLED: False,
    DISABLED_PROVIDER_KEYS: [],
    DISABLED_CAPABILITIES: [],
    # A mangled limit must not hand out free creations: fail closed.
    FREE_IMAGE_DAILY_LIMIT: 0,
}


class UnknownSettingError(Exception):
    """Raised for any key outside KNOWN_KEYS — a caller/programming error,
    never a runtime/operator condition (KNOWN_KEYS is the fixed, small set
    Etapa 5H defines; nothing here ever accepts an arbitrary new key)."""


class InvalidSettingValueError(Exception):
    """Raised internally by _validate() for a value of the wrong shape —
    always caught by get_setting() (-> fail-safe default) or propagated
    to the admin caller by set_setting() (a write is REJECTED outright,
    never persisted with a bad shape — corruption can only ever originate
    from something bypassing this module, e.g. a manual DB edit)."""


def _validate(key: str, value: Any) -> Any:
    if key in _BOOL_KEYS:
        if not isinstance(value, bool):
            raise InvalidSettingValueError(f"{key!r} must be a bool, got {type(value).__name__}")
        return value
    if key in _STRING_LIST_KEYS:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise InvalidSettingValueError(f"{key!r} must be a list of strings")
        return list(value)
    if key in _INT_KEYS:
        low, high = _INT_BOUNDS[key]
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise InvalidSettingValueError(f"{key!r} must be an integer between {low} and {high}")
        return value
    raise UnknownSettingError(key)  # pragma: no cover — every key set is covered above


def get_setting(db: DBSession, key: str) -> Any:
    """Read one setting with the fail-safe behavior documented in this
    module's docstring. Raises UnknownSettingError for a key outside
    KNOWN_KEYS — every call site in this codebase only ever asks for one
    of the constants above, so that can only mean a programming error."""
    if key not in KNOWN_KEYS:
        raise UnknownSettingError(key)

    try:
        row = db.query(RuntimeSetting).filter(RuntimeSetting.key == key).first()
    except Exception:
        db.rollback()
        logger.exception(
            "runtime_settings: failed to read key=%s — falling back to the fail-safe default", key,
        )
        return _DEFAULT_WHEN_CORRUPTED[key]

    if row is None:
        return _DEFAULT_WHEN_MISSING[key]

    try:
        return _validate(key, row.value)
    except InvalidSettingValueError:
        logger.error(
            "runtime_settings: corrupted value for key=%s — falling back to the fail-safe default", key,
        )
        return _DEFAULT_WHEN_CORRUPTED[key]


def set_setting(db: DBSession, key: str, value: Any, *, admin_user_id: str) -> None:
    """Validate + persist one setting, then log + record the
    runtime_setting_changed audit event. The ONLY write path — nothing
    else in this codebase should construct/commit a RuntimeSetting row
    directly. Raises UnknownSettingError/InvalidSettingValueError (never
    silently coerces or partially applies a bad write) so the admin route
    calling this can turn either into a clean 400/422."""
    if key not in KNOWN_KEYS:
        raise UnknownSettingError(key)
    validated = _validate(key, value)

    row = db.query(RuntimeSetting).filter(RuntimeSetting.key == key).first()
    if row is None:
        row = RuntimeSetting(key=key, value=validated, updated_by_user_id=admin_user_id)
        db.add(row)
    else:
        row.value = validated
        row.updated_by_user_id = admin_user_id
    db.commit()

    # Structured, redaction-safe (Etapa 5G's JsonFormatter/RequestIdLogFilter
    # already apply to every logger.info call — no separate audit pipeline
    # needed) — value is always a bool or a list of known enum-like
    # strings (provider catalog keys / Capability values), never user
    # content, so there is nothing here for redact() to need to catch.
    logger.info(
        "runtime_setting_changed | key=%s value=%r admin_user=%s", key, validated, admin_user_id,
    )
    _settings_changes_total.inc(key=key)


# ── Typed accessors — every OTHER call site in Core uses one of these,
# never get_setting()/KNOWN_KEYS directly. ──────────────────────────────

def is_agent_enabled(db: DBSession) -> bool:
    return bool(get_setting(db, AGENT_ENABLED))


def is_continuity_enabled(db: DBSession) -> bool:
    return bool(get_setting(db, CONTINUITY_ENABLED))


def get_disabled_provider_keys(db: DBSession) -> frozenset[str]:
    return frozenset(get_setting(db, DISABLED_PROVIDER_KEYS))


def get_disabled_capabilities(db: DBSession) -> frozenset[str]:
    return frozenset(get_setting(db, DISABLED_CAPABILITIES))


def is_provider_disabled(db: DBSession, provider_key: str) -> bool:
    return provider_key in get_disabled_provider_keys(db)


def is_capability_disabled(db: DBSession, capability_value: str) -> bool:
    return capability_value in get_disabled_capabilities(db)


def get_free_image_daily_limit(db: DBSession) -> int | None:
    """Operator override for FREE's daily images, or None for the plan table's figure."""
    return get_setting(db, FREE_IMAGE_DAILY_LIMIT)


def get_all_settings(db: DBSession) -> dict[str, Any]:
    """Effective (fail-safe-resolved) value for every known key — powers
    the admin GET endpoint. Never raises."""
    return {key: get_setting(db, key) for key in sorted(KNOWN_KEYS)}
