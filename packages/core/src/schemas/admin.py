"""Admin / Runtime Settings schemas — Etapa 5H.

Scope: the small, fixed set of operational kill switches
(src/services/runtime_settings.py::KNOWN_KEYS) — never a generic
arbitrary-config API. `value`'s shape depends on which key is being set
(bool for agent_enabled/continuity_enabled, list[str] for
disabled_provider_keys/disabled_capabilities) — validated server-side by
runtime_settings.set_setting(), not by this schema (Any here, on purpose:
a stricter pydantic union would just duplicate runtime_settings.py's own
validation in a second place).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UpdateRuntimeSettingRequest(BaseModel):
    value: Any = Field(
        ...,
        description=(
            "New value for this setting — bool for agent_enabled/"
            "continuity_enabled, list[str] for disabled_provider_keys/"
            "disabled_capabilities."
        ),
    )


class RuntimeSettingsResponse(BaseModel):
    settings: dict[str, Any]
