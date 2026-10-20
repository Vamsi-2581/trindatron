"""Object storage — one contract, two backends, one place that chooses.

build_object_store() is the only thing that decides which backend a
deployment gets, so no caller ever branches on "are we in production".
The rule is simply whether R2 is configured: credentials present means R2,
absent means the local directory.

The local directory is a DEVELOPMENT backend. On a container platform it is
ephemeral — a deck written there is gone on the next deploy — so production
must not quietly accept it as a substitute for R2. That refusal lives in
local_store_is_acceptable() below and is enforced by the capability that
depends on storage (presentation_service.availability()), which declines to
offer the capability rather than generating decks into a filesystem that
will drop them. Failing to offer is recoverable; silently losing a paid-for
artifact is not.
"""
from __future__ import annotations

from src.config import settings
from src.storage.local import LocalObjectStore
from src.storage.object_store import ObjectStore, ObjectStoreError, StoredObject
from src.storage.r2 import R2ObjectStore

__all__ = [
    "ObjectStore",
    "ObjectStoreError",
    "StoredObject",
    "R2ObjectStore",
    "LocalObjectStore",
    "build_object_store",
    "r2_is_configured",
    "local_store_is_acceptable",
]


def r2_is_configured() -> bool:
    return bool(
        settings.r2_endpoint
        and settings.r2_bucket
        and settings.r2_access_key_id
        and settings.r2_secret_access_key
    )


def local_store_is_acceptable() -> bool:
    """Whether falling back to the on-disk store is a legitimate state here.

    Everywhere but production, yes — it is how the capability is developed
    and tested without a Cloudflare account. In production, no: the container
    filesystem does not survive a deploy, so an artifact written there is a
    paid-for file that quietly disappears.
    """
    return not settings.is_production


def build_object_store() -> ObjectStore:
    """The store this deployment uses. See the module docstring for the rule."""
    if r2_is_configured():
        return R2ObjectStore(
            endpoint=settings.r2_endpoint,
            bucket=settings.r2_bucket,
            access_key_id=settings.r2_access_key_id,
            secret_access_key=settings.r2_secret_access_key,
        )
    return LocalObjectStore(root=settings.local_object_store_root)
