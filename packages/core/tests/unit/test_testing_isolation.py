"""CP8 — test infrastructure stabilization.

CP7 found the test suite's real root-cause hazards: the FastAPI lifespan a
TestClient boots is the SAME one production runs — every startup reconciliation
call and every recurring background loop (execution worker, agent scheduler,
payment order expiry, webhook nonce cleanup, entitlement reservation reclaim,
artifact retention, and CP7's own `_resume_media_provider_jobs`) started for
every single test, racing that test's own `Base.metadata.create_all()` /
`drop_all()` cycle on the shared engine. That race is what made
`tests/integration/test_admin_economics.py` fail non-deterministically. Worse,
those loops opened `SessionLocal()` directly — bound, before this fix, to
whatever `DATABASE_URL` said, i.e. the real `trinity_dev.db` — so a job a real
Higgsfield submission left `running` there could have triggered a genuine,
paid, external API call from nothing more than running `pytest`.

This file proves the fix (settings.testing, in src/config.py) actually holds:
the whole suite's database is an isolated, disposable in-memory engine, and a
TestClient boot starts none of the seven loops or four startup routines.
`test_provider_network_safety.py` proves the independent, second backstop
(no real HTTP transport reachable at all) against the exact resume code path.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import settings
from src.database import engine
from src.main import app


def test_settings_testing_is_true_for_the_whole_suite():
    """conftest.py sets TESTING=true before importing src.config — every test
    inherits this from the one process-wide Settings singleton."""
    assert settings.testing is True


def test_testing_is_refused_in_production():
    """The one thing that must NEVER be true: settings.testing=True reaching a
    real production boot. Settings._validate_production_secrets refuses it."""
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="TESTING must not be true in production"):
        settings.__class__(
            environment="production", testing=True, jwt_secret_key="a" * 64,
            anthropic_api_key="sk-ant-test", debug=False, allowed_origins=["https://app.trinity.ai"],
            database_url="postgresql+psycopg://user:pass@host:5432/trinity_db",
        )


def test_the_test_process_never_opens_the_real_dev_database():
    """settings.database_url still names trinity_dev.db (it does — read from
    packages/core/.env, unchanged) — src.database._build_engine() must ignore
    it entirely under settings.testing and build an isolated in-memory engine
    instead. Whatever code opens a Session, in this process, for the life of
    the whole pytest run, it is physically impossible for it to reach the real
    file or a real Postgres — there is no code path back to settings.database_url
    once settings.testing is true."""
    assert str(engine.url) == "sqlite:///:memory:"
    assert "trinity_dev.db" not in str(engine.url)
    assert "trinity_dev.db" in settings.database_url  # the setting is unchanged; only the engine ignores it


def _async_spy(name: str, called: list[str]):
    async def _inner(*args, **kwargs):
        called.append(name)
    return _inner


def _sync_spy(name: str, called: list[str]):
    def _inner(*args, **kwargs):
        called.append(name)
    return _inner


def test_lifespan_skips_every_reconciliation_call_and_background_loop_under_testing(db, monkeypatch):
    """Spy on every function/loop main.py's lifespan calls only `if not
    settings.testing`, boot a fresh TestClient against them, and prove none
    fired. Uses its own TestClient (not the shared `client` fixture) because
    the patches below must be in place BEFORE the lifespan runs."""
    import src.main as main_module
    import src.services.agent_schedule_service as agent_schedule_service
    import src.services.artifact_service as artifact_service
    import src.services.entitlement_service as entitlement_service
    import src.services.execution_worker as execution_worker
    import src.services.payment_order_service as payment_order_service
    import src.services.webhook_replay_service as webhook_replay_service

    called: list[str] = []

    # One-shot startup reconciliation (all four hit SessionLocal() directly).
    monkeypatch.setattr(main_module, "_run_startup_cleanup", _sync_spy("_run_startup_cleanup", called))
    monkeypatch.setattr(main_module, "_run_5i_reconciliation", _sync_spy("_run_5i_reconciliation", called))
    monkeypatch.setattr(main_module, "_run_execution_reconciliation",
                        _sync_spy("_run_execution_reconciliation", called))
    monkeypatch.setattr(main_module, "_resume_media_provider_jobs",
                        _sync_spy("_resume_media_provider_jobs", called))

    # The seven recurring background loops (main.py imports each by name, from
    # its own module, inside the lifespan function body — patching the source
    # module's attribute is what a fresh `from module import name` picks up).
    loop_targets = (
        (agent_schedule_service, "run_schedule_poll_loop"),
        (execution_worker, "run_worker_loop"),
        (execution_worker, "run_recovery_loop"),
        (payment_order_service, "run_payment_order_expiry_loop"),
        (webhook_replay_service, "run_webhook_nonce_cleanup_loop"),
        (entitlement_service, "run_tcu_reservation_reclaim_loop"),
        (artifact_service, "run_artifact_retention_loop"),
    )
    for module, attr in loop_targets:
        monkeypatch.setattr(module, attr, _async_spy(attr, called))

    with TestClient(main_module.app, raise_server_exceptions=True) as c:
        assert c.get("/health").status_code == 200

    assert called == [], f"these production-only routines ran during a test boot: {called}"
