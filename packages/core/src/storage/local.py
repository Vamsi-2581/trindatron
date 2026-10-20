"""LocalObjectStore — a directory on disk, for development and tests.

Exists so the presentation capability can be built, wired and tested end to
end before R2 credentials arrive, and so a developer running the stack on a
laptop is not required to own a Cloudflare account.

It is honest about the one thing it cannot do: mint a URL a browser can fetch
directly. signed_url() returns None, and the download route streams the bytes
through the API instead. That is a deliberately worse-performing path chosen
over the alternative of inventing a URL scheme that only works locally and
then silently diverges from production behaviour.

Path safety
-----------
Keys are attacker-influenced in principle (they embed a user id and a job id),
so every key is resolved and then checked to still be inside the root. A key
of "../../etc/passwd" resolves outside and is refused rather than written.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from src.storage.object_store import ObjectStore, ObjectStoreError, StoredObject

logger = logging.getLogger("trinity.storage.local")


class LocalObjectStore(ObjectStore):
    def __init__(self, *, root: str) -> None:
        self._root = Path(root).resolve()

    @property
    def backend_name(self) -> str:
        return "local"

    def _path(self, key: str) -> Path:
        candidate = (self._root / key.lstrip("/")).resolve()
        # Containment check, not a string prefix test: `.resolve()` has
        # already collapsed any traversal, so this compares real paths.
        if candidate != self._root and self._root not in candidate.parents:
            raise ObjectStoreError("refusing a key that resolves outside the storage root")
        return candidate

    async def is_available(self) -> bool:
        try:
            await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
            return True
        except OSError as exc:
            logger.warning("local store: unusable root | %s", type(exc).__name__)
            return False

    async def put(self, *, key: str, data: bytes, content_type: str) -> StoredObject:
        path = self._path(key)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            raise ObjectStoreError(f"local write failed: {type(exc).__name__}") from exc
        return StoredObject(
            key=key, size_bytes=len(data), content_type=content_type, backend=self.backend_name
        )

    async def get(self, *, key: str) -> bytes:
        path = self._path(key)
        try:
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ObjectStoreError(f"local read failed: {type(exc).__name__}") from exc

    async def delete(self, *, key: str) -> None:
        path = self._path(key)
        try:
            await asyncio.to_thread(path.unlink, True)
        except OSError as exc:
            raise ObjectStoreError(f"local delete failed: {type(exc).__name__}") from exc

    async def signed_url(
        self, *, key: str, expires_in: int, filename: str | None = None
    ) -> str | None:
        """No direct URL exists for a local directory. See the module
        docstring — the caller streams instead."""
        return None
