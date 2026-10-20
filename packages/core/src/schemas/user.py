from __future__ import annotations

from pydantic import BaseModel, field_validator

from src.timezones import is_valid_iana_timezone


class UpdateTimezoneRequest(BaseModel):
    """Body for PATCH /users/me/timezone.

    timezone must be a real IANA zone name (e.g. "America/Sao_Paulo") —
    arbitrary strings are rejected with 422, never silently accepted.
    """

    timezone: str

    @field_validator("timezone")
    @classmethod
    def validate_iana_timezone(cls, v: str) -> str:
        if not is_valid_iana_timezone(v):
            raise ValueError(f"{v!r} is not a valid IANA timezone")
        return v


class TimezoneResponse(BaseModel):
    timezone: str | None
