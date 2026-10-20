"""SigV4 signer checked against AWS's own published vectors.

There are no R2 credentials in this environment, so a live round trip cannot
prove this module. What can prove it is the specification's own worked
examples: AWS documents the exact expected signature for a presigned GET, for
a signed PUT, and for the intermediate signing-key derivation. If this module
reproduces all three byte for byte, the algorithm is right — and every one of
these is a value published by AWS, not one recorded from this implementation
(a self-recorded expectation would only prove the code equals itself).
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.storage import sigv4

# The canonical credentials AWS uses throughout its SigV4 documentation.
ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
SIGNING_TIME = datetime(2013, 5, 24, 0, 0, 0, tzinfo=timezone.utc)


def test_signing_key_matches_the_published_derivation_vector():
    """AWS documents this exact derived key for (iam, us-east-1, 20120215).

    Note the secret differs by one character from the S3 examples above:
    AWS's key-derivation page uses ...MDENG+bPx..., its S3 pages use
    ...MDENG/bPx.... They are two different published vectors, not a typo,
    and mixing them up yields a perfectly valid signature for the wrong key.
    """
    derivation_secret = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
    derived = sigv4.signing_key(derivation_secret, "20120215", "us-east-1", "iam")
    assert derived.hex() == "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d"


def test_presigned_get_matches_the_published_s3_vector():
    """AWS's worked "presigned URL" example for GET examplebucket/test.txt."""
    url = sigv4.presign_url(
        method="GET",
        endpoint="https://examplebucket.s3.amazonaws.com",
        host="examplebucket.s3.amazonaws.com",
        key="test.txt",
        expires_in=86400,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region="us-east-1",
        now=SIGNING_TIME,
    )
    assert (
        "X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
        in url
    )


def test_signed_put_matches_the_published_s3_vector():
    """AWS's worked "PUT object" example — note the key contains a `$`, which
    is precisely the kind of character a lax URI encoder gets wrong."""
    headers = sigv4.sign_request(
        method="PUT",
        host="examplebucket.s3.amazonaws.com",
        key="test$file.text",
        query_params=None,
        headers={"date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        payload=b"Welcome to Amazon S3.",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region="us-east-1",
        now=SIGNING_TIME,
    )
    assert (
        "Signature=98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"
        in headers["Authorization"]
    )


def test_uri_encoding_is_stricter_than_urllib_default():
    """The unreserved set is exactly A-Z a-z 0-9 - _ . ~ — everything else is
    percent-encoded. `quote`'s own default would leave several of these
    alone, which is the classic source of silent signature mismatches."""
    assert sigv4.canonical_uri("a b+c/d~e.f") == "/a%20b%2Bc/d~e.f"


def test_presigned_url_never_contains_the_secret():
    url = sigv4.presign_url(
        method="GET",
        endpoint="https://acct.r2.cloudflarestorage.com",
        host="acct.r2.cloudflarestorage.com",
        key="presentations/u1/deck.pptx",
        expires_in=300,
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
        region="auto",
        now=SIGNING_TIME,
    )
    assert SECRET_KEY not in url
    # The access key IS present — the format requires it and it is public.
    assert ACCESS_KEY in url
    assert "X-Amz-Expires=300" in url


def test_canonical_query_sorts_by_encoded_key():
    assert sigv4.canonical_query({"b": "2", "a": "1"}) == "a=1&b=2"
