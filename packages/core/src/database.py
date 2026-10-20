from datetime import timezone as _tz
from typing import Generator

from sqlalchemy import DateTime, create_engine, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.types import TypeDecorator

from src.config import settings


def _normalize_pg_url(url: str) -> str:
    # Railway/Render/Heroku supply postgresql:// or postgres://, but SQLAlchemy
    # maps those to the psycopg2 dialect. psycopg3 (installed) needs the explicit
    # postgresql+psycopg:// scheme. Rewrite at engine creation so DATABASE_URL
    # env var doesn't need to change.
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _build_engine():
    """Create the SQLAlchemy engine with dialect-appropriate settings."""
    if settings.testing:
        # CP8: tests never open DATABASE_URL, whatever it is set to — an isolated,
        # disposable in-memory database, structurally impossible to confuse with the
        # real trinity_dev.db (or a real Postgres). StaticPool: SQLite's ":memory:"
        # database lives only on one connection, and every Session (this module's
        # get_db() included) must reuse that same one for tables created by one
        # session to be visible to another — tests/conftest.py's db/client fixtures
        # rely on exactly this to see each other's commits.
        from sqlalchemy.pool import StaticPool

        return create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            echo=settings.debug,
        )

    url = _normalize_pg_url(settings.database_url)
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        # SQLite: single-threaded by default — override for multi-threaded test/dev use.
        # No connection pool needed; SQLAlchemy uses SingletonThreadPool for SQLite.
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=settings.debug,
        )

    # PostgreSQL via psycopg3 (postgresql+psycopg://):
    # Configure a connection pool suitable for production load.
    return create_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=True,   # detect stale connections before handing them out
        echo=settings.debug,
    )


engine = _build_engine()

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


class UTCDateTime(TypeDecorator):
    """DateTime(timezone=True) that stays timezone-aware (UTC) on every dialect.

    PostgreSQL preserves tzinfo natively on round-trip — a no-op there.
    SQLite (tests, and any SQLite dev use) silently drops tzinfo even for
    DateTime(timezone=True) columns, returning naive datetimes on read;
    that breaks any Python-level comparison against an aware "now" with
    "can't compare offset-naive and offset-aware datetimes". This
    codebase only ever stores UTC, so reattaching UTC tzinfo on a naive
    read is always correct, never a guess.

    Use this instead of plain DateTime(timezone=True) for any column whose
    value is compared in Python (not just filtered in a DB query) — e.g.
    EntitlementSession's session_started_at/session_expires_at.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=_tz.utc)
        return value


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_db_connection() -> bool:
    """Return True if a database connection can be established, False otherwise."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
