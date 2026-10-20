"""AWS Signature Version 4 — the part of S3 compatibility that has to be exact.

Cloudflare R2 speaks the S3 API, and the S3 API authenticates with SigV4.
Two operations need it here: PUT an object, and mint a presigned GET URL a
browser can follow without ever seeing a credential.

Why this is hand-written rather than boto3
-------------------------------------------
boto3 would add botocore (~80 MB installed) for exactly those two calls, and
it would not make the result more verifiable here: there are no R2
credentials in this environment, so a live round trip cannot be run either
way. What CAN be run offline is the canonical proof — AWS publishes exact
expected outputs for every stage of the algorithm, and tests/unit/
test_sigv4.py asserts this module reproduces them byte for byte, including
the presigned-URL vector. A signer checked against the specification's own
vectors is better evidence than an unexercised dependency.

Nothing here reads configuration, logs, or holds state. Credentials arrive as
arguments, are used inside HMAC, and never appear in a returned string except
as the public access-key half of X-Amz-Credential — which the signed-URL
format requires and which is not the secret.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from urllib.parse import quote

ALGORITHM = "AWS4-HMAC-SHA256"
# S3 accepts this in place of a body hash when the payload is not signed —
# required for presigned URLs, where there is no body at all.
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    """RFC 3986 encoding as SigV4 defines it.

    Deliberately stricter than urllib's default: SigV4 requires the unreserved
    set to be A-Z a-z 0-9 - _ . ~ with everything else percent-encoded in
    uppercase hex. `quote`'s default safe set differs, and one character
    encoded differently from the server's expectation produces a signature
    mismatch with no useful error message.
    """
    safe = "-_.~" + ("" if encode_slash else "/")
    return quote(value, safe=safe)


def canonical_uri(key: str) -> str:
    """Object key -> canonical URI path. Segments are encoded, separators are
    not."""
    return "/" + "/".join(_uri_encode(seg, encode_slash=True) for seg in key.split("/"))


def canonical_query(params: dict[str, str]) -> str:
    """Sorted, encoded, ampersand-joined. Sorting is by the ENCODED key, which
    is what the specification says and what differs from sorting the raw key
    once non-ASCII names are involved."""
    encoded = sorted(
        (_uri_encode(k, encode_slash=True), _uri_encode(v, encode_slash=True))
        for k, v in params.items()
    )
    return "&".join(f"{k}={v}" for k, v in encoded)


def signing_key(secret_key: str, date_stamp: str, region: str, service: str) -> bytes:
    """The four-stage derived key. Scoped to one day, one region and one
    service, so a leaked signing key is worth far less than the secret it came
    from."""
    k_date = _hmac(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac(k_date, region)
    k_service = _hmac(k_region, service)
    return _hmac(k_service, "aws4_request")


def credential_scope(date_stamp: str, region: str, service: str) -> str:
    return f"{date_stamp}/{region}/{service}/aws4_request"


def build_canonical_request(
    *,
    method: str,
    uri: str,
    query: str,
    headers: dict[str, str],
    payload_hash: str,
) -> tuple[str, str]:
    """Returns (canonical_request, signed_headers).

    Header names are lowercased and sorted; values have internal whitespace
    collapsed. Only the headers passed in are signed — the caller decides that
    set, and every one of them must go on the wire exactly as signed.
    """
    lowered = {k.lower().strip(): " ".join(str(v).split()) for k, v in headers.items()}
    signed_headers = ";".join(sorted(lowered))
    canonical_headers = "".join(f"{k}:{lowered[k]}\n" for k in sorted(lowered))
    canonical_request = "\n".join(
        [method.upper(), uri, query, canonical_headers, signed_headers, payload_hash]
    )
    return canonical_request, signed_headers


def string_to_sign(*, amz_date: str, scope: str, canonical_request: str) -> str:
    return "\n".join(
        [ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )


def _signature(secret_key: str, date_stamp: str, region: str, service: str, to_sign: str) -> str:
    return hmac.new(
        signing_key(secret_key, date_stamp, region, service),
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def sign_request(
    *,
    method: str,
    host: str,
    key: str,
    query_params: dict[str, str] | None,
    headers: dict[str, str],
    payload: bytes,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    now: datetime | None = None,
) -> dict[str, str]:
    """Sign with the Authorization-header form (used for PUT and DELETE).

    Returns the headers to ADD. The caller must send them together with the
    ones it passed in — the signature covers exactly that set.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    payload_hash = _sha256_hex(payload)

    signed_input = dict(headers)
    signed_input["host"] = host
    signed_input["x-amz-date"] = amz_date
    signed_input["x-amz-content-sha256"] = payload_hash

    canonical_request, signed_headers = build_canonical_request(
        method=method,
        uri=canonical_uri(key),
        query=canonical_query(query_params or {}),
        headers=signed_input,
        payload_hash=payload_hash,
    )
    scope = credential_scope(date_stamp, region, service)
    to_sign = string_to_sign(amz_date=amz_date, scope=scope, canonical_request=canonical_request)
    signature = _signature(secret_key, date_stamp, region, service, to_sign)

    authorization = (
        f"{ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Authorization": authorization,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }


def presign_url(
    *,
    method: str,
    endpoint: str,
    host: str,
    key: str,
    expires_in: int,
    access_key: str,
    secret_key: str,
    region: str,
    service: str = "s3",
    extra_query: dict[str, str] | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a URL that carries its own authorization in the query string.

    This is what reaches the user's browser. It grants exactly one method on
    exactly one key for exactly `expires_in` seconds, and it contains no
    secret — the signature is a one-way function of one, and the credential
    field is only the public access-key id.
    """
    now = now or datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = credential_scope(date_stamp, region, service)

    params: dict[str, str] = dict(extra_query or {})
    params.update(
        {
            "X-Amz-Algorithm": ALGORITHM,
            "X-Amz-Credential": f"{access_key}/{scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(int(expires_in)),
            "X-Amz-SignedHeaders": "host",
        }
    )

    canonical_request, _ = build_canonical_request(
        method=method,
        uri=canonical_uri(key),
        query=canonical_query(params),
        headers={"host": host},
        payload_hash=UNSIGNED_PAYLOAD,
    )
    to_sign = string_to_sign(amz_date=amz_date, scope=scope, canonical_request=canonical_request)
    signature = _signature(secret_key, date_stamp, region, service, to_sign)

    query = canonical_query(params) + f"&X-Amz-Signature={signature}"
    return f"{endpoint.rstrip('/')}{canonical_uri(key)}?{query}"
