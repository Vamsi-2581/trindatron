"""The settings a person can change, and nothing else.

Every field has a safe default, so a person who never opened Settings behaves
exactly as before. Unknown keys are refused rather than stored: a setting that
nothing reads would look like a control and do nothing.

Deliberately absent until something honours them: a colour theme (the apps ship
one palette) and a spoken-input language (voice transcription takes no language
hint). A value stored under either name by an earlier version is ignored.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Language = Literal["auto", "pt", "en", "es"]
ResponseStyle = Literal["balanced", "concise", "detailed"]


class Preferences(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # General
    language: Language = "auto"
    response_style: ResponseStyle = "balanced"
    # Clients: Enter sends (Shift+Enter breaks the line); off, Ctrl/Cmd+Enter sends
    send_on_enter: bool = True
    # Voice: read each finished answer aloud when voice output is available
    voice_auto_play: bool = False
    # Privacy / memory
    memory_enabled: bool = True
    # Plugins the person switched off (ids from src/integrations/catalog.py)
    disabled_plugins: list[str] = Field(default_factory=list)


class PreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Language | None = None
    response_style: ResponseStyle | None = None
    send_on_enter: bool | None = None
    voice_auto_play: bool | None = None
    memory_enabled: bool | None = None
    disabled_plugins: list[str] | None = None
