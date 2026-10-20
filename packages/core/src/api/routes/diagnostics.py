"""Development-only diagnostics: a client reports how its own startup went.

Startup is measured on the device (launch, JS bundle, session restore, profile,
capabilities, first Home render, composer usable). The client can only keep
those numbers to itself, so a development build posts them here and they are
written to the Core log, where they can be read and compared before and after a
change. This router is never mounted when ENVIRONMENT=production (src/main.py),
and it accepts only numbers under bounded names.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import APIRouter, Response, status
from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger("trinity.diagnostics")

router = APIRouter(prefix="/diagnostics", tags=["diagnostics"])

_NAME = re.compile(r"^[A-Za-z0-9_]{1,40}$")


class ClientTiming(BaseModel):
    platform: str = Field(min_length=1, max_length=16)
    app_version: str | None = Field(default=None, max_length=32)
    origin: str = Field(default="unknown", max_length=16)
    marks: dict[str, float | None] = Field(default_factory=dict)
    durations: dict[str, float] = Field(default_factory=dict)

    @field_validator("marks", "durations")
    @classmethod
    def _bounded(cls, value: dict) -> dict:
        if len(value) > 32 or any(not _NAME.match(str(key)) for key in value):
            raise ValueError("unexpected timing names")
        return value


@router.post("/client-timing", status_code=status.HTTP_204_NO_CONTENT)
def client_timing(body: ClientTiming) -> Response:
    logger.info("client_timing | %s", json.dumps(body.model_dump(), sort_keys=True))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
