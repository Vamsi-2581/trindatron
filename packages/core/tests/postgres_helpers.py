"""
Helpers for tests that require a REAL PostgreSQL instance — not SQLite.

Gated by TEST_POSTGRES_URL, an isolated, test-only PostgreSQL database.
This is intentionally a SEPARATE env var from DATABASE_URL: the main test
suite (conftest.py's `db`/`client` fixtures) stays on SQLite for every
other test, unaffected by whether TEST_POSTGRES_URL is set. Tests gated by
postgres_available() skip gracefully (with an accurate reason) when no
PostgreSQL is reachable, rather than failing the whole suite.

To run the PostgreSQL-gated tests locally:
    docker run -d --name trinity-test-postgres \
      -e POSTGRES_USER=trinity_test -e POSTGRES_PASSWORD=trinity_test_pw \
      -e POSTGRES_DB=trinity_test -p 55432:5432 postgres:16-alpine

    TEST_POSTGRES_URL=postgresql+psycopg://trinity_test:trinity_test_pw@localhost:55432/trinity_test \
      uv run pytest -k postgresql
"""
from __future__ import annotations

import os


def get_test_postgres_url() -> str | None:
    return os.environ.get("TEST_POSTGRES_URL")


def postgres_available() -> bool:
    """True if TEST_POSTGRES_URL is set AND a real connection succeeds.

    Never raises — any connection failure (unreachable host, wrong
    credentials, driver missing) is treated as "not available", so the
    gated tests skip cleanly instead of erroring at collection time.
    """
    url = get_test_postgres_url()
    if not url:
        return False
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(url)
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        finally:
            engine.dispose()
    except Exception:
        return False
