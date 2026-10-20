"""R2ObjectStore — Cloudflare R2 over the S3 API.

R2 is the production store for generated artifacts. It is S3-compatible, so
everything here is ordinary S3: SigV4 (src/storage/sigv4.py, proven against
AWS's published vectors), a PUT to write, a presigned GET to read.

R2 specifics that are not S3 defaults
-------------------------------------
  region       always "auto". R2 has no user-visible regions; the signature
               still needs a region string and this is the one R2 expects.
  endpoint     https://<account-id>.r2.cloudflarestorage.com — the bucket is
               the first path segment, not a subdomain (path-style), so keys
               are signed as "<bucket>/<key>".
  egress       free, which is the whole reason R2 is preferred over S3 here:
               a deck is downloaded at least once per generation and egress
               is where S3-style pricing actually bites.

The bucket is PRIVATE. Nothing here ever makes an object public, and there is
no code path that produces a non-expiring URL — signed_url() takes a
mandatory expiry and passes it into the signature, so the URL stops working
whether or not anyone remembers to clean up.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx

from src.storage import sigv4
from src.storage.object_store import ObjectStore, ObjectStoreError, StoredObject

logger = logging.getLogger("trinity.storage.r2")

# R2 has no regions; the S3 signing algorithm still requires a region string.
R2_REGION = "auto"


class R2ObjectStore(ObjectStore):
    def __init__(
        self,
        *,
        endpoint: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        timeout_s: float = 60.0,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._bucket = bucket
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._timeout_s = timeout_s

    @property
    def backend_name(self) -> str:
        return "r2"

    @property
    def _host(self) -> str:
        return urlsplit(self._endpoint).netloc

    def _full_key(self, key: str) -> str:
        """Path-style addressing: the bucket is part of the signed path."""
        return f"{self._bucket}/{key.lstrip('/')}"

    def _configured(self) -> bool:
        return bool(
            self._endpoint and self._bucket and self._access_key_id and self._secret_access_key
        )

    async def is_available(self) -> bool:
        """Configured AND reachable. A HEAD on the bucket costs nothing and
        distinguishes "credentials missing" from "credentials wrong", which
        matters because only one of those is an operator's mistake to fix
        now."""
        if not self._configured():
            return False
        try:
            key = f"{self._bucket}/"
            headers = sigv4.sign_request(
                method="HEAD",
                host=self._host,
                key=key,
                query_params=None,
                headers={},
                payload=b"",
                access_key=self._access_key_id,
                secret_key=self._secret_access_key,
                region=R2_REGION,
            )
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.head(f"{self._endpoint}/{self._bucket}/", headers=headers)
            # 200 = bucket there. 404 = endpoint reachable, bucket name wrong.
            # 403 = credentials rejected. Only 200 means "usable".
            if r.status_code != 200:
                logger.warning("r2: bucket probe returned %s", r.status_code)
            return r.status_code == 200
        except Exception as exc:  # noqa: BLE001 — availability never raises
            logger.warning("r2: unreachable | %s", type(exc).__name__)
            return False

    async def put(self, *, key: str, data: bytes, content_type: str) -> StoredObject:
        full_key = self._full_key(key)
        headers = sigv4.sign_request(
            method="PUT",
            host=self._host,
            key=full_key,
            query_params=None,
            headers={"content-type": content_type},
            payload=data,
            access_key=self._access_key_id,
            secret_key=self._secret_access_key,
            region=R2_REGION,
        )
        headers["content-type"] = content_type
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                r = await client.put(f"{self._endpoint}/{full_key}", content=data, headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"upload failed: {type(exc).__name__}") from exc
        if r.status_code not in (200, 201):
            # Status only. An S3 error body echoes request metadata including
            # the key and the access-key id, none of which belongs in a log.
            raise ObjectStoreError(f"upload rejected ({r.status_code})")
        logger.info("r2: stored | key=%s bytes=%d", key, len(data))
        return StoredObject(
            key=key, size_bytes=len(data), content_type=content_type, backend=self.backend_name
        )

    async def get(self, *, key: str) -> bytes:
        full_key = self._full_key(key)
        headers = sigv4.sign_request(
            method="GET",
            host=self._host,
            key=full_key,
            query_params=None,
            headers={},
            payload=b"",
            access_key=self._access_key_id,
            secret_key=self._secret_access_key,
            region=R2_REGION,
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                r = await client.get(f"{self._endpoint}/{full_key}", headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"download failed: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise ObjectStoreError(f"download rejected ({r.status_code})")
        return r.content

    async def delete(self, *, key: str) -> None:
        full_key = self._full_key(key)
        headers = sigv4.sign_request(
            method="DELETE",
            host=self._host,
            key=full_key,
            query_params=None,
            headers={},
            payload=b"",
            access_key=self._access_key_id,
            secret_key=self._secret_access_key,
            region=R2_REGION,
        )
        try:
            async with httpx.AsyncClient(timeout=self._timeout_s) as client:
                r = await client.delete(f"{self._endpoint}/{full_key}", headers=headers)
        except Exception as exc:  # noqa: BLE001
            raise ObjectStoreError(f"delete failed: {type(exc).__name__}") from exc
        # 204 = deleted, 404 = already gone. Both are the caller's desired
        # end state, so both succeed — delete is idempotent by contract.
        if r.status_code not in (200, 204, 404):
            raise ObjectStoreError(f"delete rejected ({r.status_code})")

    async def signed_url(
        self, *, key: str, expires_in: int, filename: str | None = None
    ) -> str | None:
        extra: dict[str, str] = {}
        if filename:
            # Makes the browser save it under the deck's name rather than the
            # opaque storage key. Part of the signature, so a user cannot
            # rewrite it to something else on a URL they were given.
            safe = filename.replace('"', "")
            extra["response-content-disposition"] = f'attachment; filename="{safe}"'
        return sigv4.presign_url(
            method="GET",
            endpoint=self._endpoint,
            host=self._host,
            key=self._full_key(key),
            expires_in=expires_in,
            access_key=self._access_key_id,
            secret_key=self._secret_access_key,
            region=R2_REGION,
            extra_query=extra,
        )
