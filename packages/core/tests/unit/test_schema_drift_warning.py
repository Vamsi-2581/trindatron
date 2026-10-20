"""The boot-time answer to "why is everything temporarily unavailable?".

A database left behind a migration fails every write that touches the new
column, and every client reports that as a capability being down. main.py says
it once, plainly, at startup — and never breaks boot to do so.

The head is read from the migration files on purpose: this package ships a
directory named `alembic/`, so importing the library by name is not reliable
here, and a check that silently never runs is worse than no check.
"""
from __future__ import annotations

import logging

from src import main


def test_the_head_is_the_revision_nothing_supersedes(tmp_path):
    (tmp_path / "0001_first.py").write_text('revision = "0001_first"\ndown_revision = None\n', encoding="utf-8")
    (tmp_path / "0002_second.py").write_text('revision = "0002_second"\ndown_revision = "0001_first"\n', encoding="utf-8")
    (tmp_path / "0003_third.py").write_text('revision = "0003_third"\ndown_revision = "0002_second"\n', encoding="utf-8")
    assert main._migration_head(tmp_path) == "0003_third"


def test_the_real_migrations_have_exactly_one_head():
    assert main._migration_head() is not None, "two heads (or none) would make 'upgrade head' ambiguous"


def test_a_database_behind_the_migrations_is_named_at_boot(monkeypatch, caplog):
    monkeypatch.setattr(main, "_migration_head", lambda: "0041_future")
    monkeypatch.setattr(main, "_database_revision", lambda: "0040_user_preferences")
    with caplog.at_level(logging.ERROR):
        main._log_schema_drift()
    said = " ".join(record.getMessage() for record in caplog.records)
    assert "alembic upgrade head" in said and "0040_user_preferences" in said and "0041_future" in said


def test_a_database_at_the_head_says_nothing(monkeypatch, caplog):
    monkeypatch.setattr(main, "_migration_head", lambda: "0040_user_preferences")
    monkeypatch.setattr(main, "_database_revision", lambda: "0040_user_preferences")
    with caplog.at_level(logging.ERROR):
        main._log_schema_drift()
    assert not [record for record in caplog.records if "[SCHEMA]" in record.getMessage()]


def test_it_never_breaks_boot(monkeypatch, caplog):
    def explode():
        raise RuntimeError("no database here")

    monkeypatch.setattr(main, "_database_revision", explode)
    with caplog.at_level(logging.ERROR):
        main._log_schema_drift()  # must not raise
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
