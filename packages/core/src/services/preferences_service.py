"""Read and change a person's preferences, and turn them into behaviour.

Preferences are per person and reach a request through TaskContext (language,
response style), memory selection (memory_enabled) and the agent's tool list
(disabled_plugins) — never through the global policy, which is the same for
everyone (src/prompting/policy.py).

A preference can only narrow what the product does for that person: switching a
plugin off removes its tools; nothing here can switch on a tool, a capability or
an approval the plan and the manifest do not already allow.
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session as DBSession

from src.models.user_preferences import UserPreferences

logger = logging.getLogger(__name__)
from src.schemas.preferences import Preferences, PreferencesUpdate

LANGUAGE_NAMES = {"pt": "Portuguese (Brazil)", "en": "English", "es": "Spanish"}
STYLE_INSTRUCTIONS = {
    "balanced": "",
    "concise": "This person prefers short, direct answers. Keep responses brief unless the task itself needs more.",
    "detailed": "This person prefers thorough answers. Include context, reasoning and relevant detail when it helps.",
}


class PreferencesRefused(ValueError):
    """An update that names something that does not exist."""


def get_preferences(db: DBSession, user_id: str) -> Preferences:
    row = db.get(UserPreferences, user_id)
    stored = row.data if row is not None and isinstance(row.data, dict) else {}
    try:
        return Preferences(**stored)
    except Exception:
        # A stored value that no longer validates (a removed option) falls back to defaults, key by key.
        clean = {}
        for key, value in stored.items():
            try:
                Preferences(**{key: value})
                clean[key] = value
            except Exception:
                continue
        return Preferences(**clean)


def update_preferences(db: DBSession, user_id: str, update: PreferencesUpdate) -> Preferences:
    from src.integrations.catalog import PLUGIN_IDS

    changes = update.model_dump(exclude_none=True)
    if "disabled_plugins" in changes:
        unknown = sorted(set(changes["disabled_plugins"]) - PLUGIN_IDS)
        if unknown:
            raise PreferencesRefused(f"unknown plugins: {', '.join(unknown)}")
        changes["disabled_plugins"] = sorted(set(changes["disabled_plugins"]))
    merged = Preferences(**{**get_preferences(db, user_id).model_dump(), **changes})
    row = db.get(UserPreferences, user_id)
    if row is None:
        row = UserPreferences(user_id=user_id, data=merged.model_dump())
        db.add(row)
    else:
        row.data = merged.model_dump()
    db.commit()
    return merged


def memory_enabled(db: DBSession, user_id: str) -> bool:
    return get_preferences(db, user_id).memory_enabled


def disabled_plugins(db: DBSession, user_id: str) -> set[str]:
    return set(get_preferences(db, user_id).disabled_plugins)


def prompt_language(preferences: Preferences) -> str:
    """The language to answer in when the person chose one; empty means "mirror the person"."""
    return LANGUAGE_NAMES.get(preferences.language, "")


def prompt_style(preferences: Preferences) -> str:
    return STYLE_INSTRUCTIONS.get(preferences.response_style, "")


def answer_language(db: DBSession, user_id: str, client_locale: str | None = None) -> str:
    """pt/en/es for this person — what they chose in Settings, else their device — or "" when neither is known.

    Used by everything that answers without a model; on "" each of those reads the text with its own hints
    (src/language.py). A failure to read the preferences must not break the turn, so it falls back to the device.
    """
    from src.language import known_language

    preference = None
    try:
        preference = get_preferences(db, user_id).language
    except Exception:
        logger.warning("preferences: answer language unavailable", exc_info=True)
    return known_language(preference, client_locale)
