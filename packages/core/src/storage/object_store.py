"""ObjectStore — where large generated files live, which is not Postgres.

A deck is a 1-10 MB PPTX and, usually, a PDF beside it. ProjectFile.content
is a LargeBinary column, so putting artifacts there would work right up until
it didn't: every row read drags the blob through the connection, backups grow
without bound, and the database becomes a file server that cannot mint a URL.
Artifacts go to object storage; the database keeps only the metadata needed to
find, authorize and expire them.

Two backends implement this contract:

  R2ObjectStore     Cloudflare R2 over the S3 API. Private bucket, presigned
                    GET URLs with a short expiry. The production answer.
  LocalObjectStore  A directory on disk. For development and tests, where no
                    R2 credentials exist. It cannot mint a URL a browser can
                    reach directly, and says so by returning None from
                    signed_url() — which is the whole reason that method is
                    Optional rather than mandatory.

signed_url() returning None is a real, supported answer, not a failure: the
download route falls back to streaming the bytes through the API when there
is no direct URL. That keeps one storage contract instead of forcing a
fabricated URL out of a backend that has none.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class ObjectStoreError(Exception):
    """A storage operation failed. Never carries credential material — the
    concrete backends deliberately log status codes rather than response
    bodies, because an S3-compatible error body can echo request metadata."""


@dataclass(frozen=True)
class StoredObject:
    """What a successful put() produced."""

    key: str
    size_bytes: int
    content_type: str
    backend: str


class ObjectStore(ABC):
    """Stores bytes under a key and hands out time-limited read access."""

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """Stable identifier persisted on the artifact row ("r2", "local"), so
        a row written by one backend is never read by another."""

    @abstractmethod
    async def is_available(self) -> bool:
        """Whether this store is configured and usable. Fail-closed: an
        unconfigured store reports False rather than raising at write time,
        so the capability that depends on it can decline before doing work."""

    @abstractmethod
    async def put(self, *, key: str, data: bytes, content_type: str) -> StoredObject:
        """Write an object. Overwrites an existing key."""

    @abstractmethod
    async def get(self, *, key: str) -> bytes:
        """Read an object back. Used by the streaming download fallback."""

    @abstractmethod
    async def delete(self, *, key: str) -> None:
        """Remove an object. Idempotent — deleting an absent key succeeds."""

    @abstractmethod
    async def signed_url(self, *, key: str, expires_in: int, filename: str | None = None) -> str | None:
        """A time-limited URL for a direct GET, or None if this backend has
        none (see the module docstring). `filename` sets the download name via
        response-content-disposition where the backend supports it."""
