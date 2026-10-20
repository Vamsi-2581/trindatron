"""Google OAuth client IDs for the native apps (Android, iOS) and the Expo web build.

Kept apart from src/config.py (held byte-for-byte with the Presenton work).
Client IDs are public values, but they must come from the Google Cloud project
that owns the app — nothing here is invented. With none set, native Google
sign-in reports itself unavailable.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class GoogleMobileSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    google_android_client_id: str = ""
    google_ios_client_id: str = ""
    google_mobile_web_client_id: str = ""

    def audiences(self) -> tuple[str, ...]:
        return tuple(v for v in (self.google_android_client_id, self.google_ios_client_id,
                                 self.google_mobile_web_client_id) if v)


@lru_cache
def google_mobile_settings() -> GoogleMobileSettings:
    return GoogleMobileSettings()
