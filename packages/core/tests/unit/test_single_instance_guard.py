"""The deployment stays single-instance until shared state exists.

Every economic control is already safe across replicas: windows, the FREE
plan-wide budget and the TEAM pool are sums over PostgreSQL under advisory
locks. What is NOT shared is process memory: request_control's rate limits
and concurrency caps, the signup/login limiters, the Agent Scheduler's slots
and the spend guard's alert de-duplication. With two replicas each of those
silently doubles (signups per IP, concurrent streams per user, alerts).

So the rule is enforced where scaling actually happens — the deploy config.
Scaling out means adding a shared store (Redis) first; this test is the
reminder, and it fails on the change that would forget it.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]


def _railway() -> dict:
    return tomllib.loads((REPO_ROOT / "railway.toml").read_text(encoding="utf-8"))


def test_railway_runs_one_replica():
    deploy = _railway().get("deploy", {})
    assert deploy.get("numReplicas", 1) == 1, (
        "More than one replica multiplies every in-process limiter; add Redis-backed "
        "request control and alert de-duplication before scaling out."
    )
    for region, config in (deploy.get("multiRegionConfig") or {}).items():
        assert (config or {}).get("numReplicas", 1) == 1, region


def test_the_server_runs_one_worker_process():
    start = _railway().get("deploy", {}).get("startCommand", "")
    assert "--workers" not in start, "uvicorn workers are separate processes with separate limiter memory"
