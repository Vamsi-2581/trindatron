# TRINDATRON Execution Runtime

> Status: implemented, integrated, tested. Runs on the existing Docker
> sandbox already used by TRINITY CODE — no new external dependency, no
> new infrastructure. Dispatch is now **Durable Execution**: a DB-backed
> queue with atomic claim/lease/heartbeat/recovery (Postgres in
> production, no Redis/Celery/external queue) — see section "Durable
> Execution / Worker Model" below for the full model. `create_job()` only
> ever persists a row; a separate worker (in-process background task
> today, but correct as-is for a future separate process/replica — the
> claim mechanism is DB-level atomic, not in-memory) claims and runs jobs
> independently of any HTTP request's lifetime, and jobs survive a
> process restart or worker crash.

## 1. What this is

The first real infrastructure letting TRINDATRON execute autonomous work
in an isolated environment, independent of the HTTP connection that
requested it: `POST /execution/jobs` returns immediately with a `job_id`;
the work continues in the background; the client polls
`GET /execution/jobs/{id}` for status whenever it reconnects — including
minutes or hours later.

```
TRINDATRON CORE
    |
    v
create_job() (src/services/execution_service.py) -- validates, admits
    |                                                (capacity/capability),
    |                                                PERSISTS status="queued".
    |                                                Never executes anything.
    v
ExecutionJob row (Postgres/SQLite) <====== the durable source of truth
    ^
    | claim (atomic UPDATE...WHERE status IN ('queued','retrying'))
    |
DURABLE WORKER (src/services/execution_worker.py) -- poll loop, independent
    |                                                 of any HTTP request
    +---- ExecutionProvider (src/execution/provider.py — ABC)
    |         +-- DockerExecutionProvider (job_type="shell", wraps trinity_sandbox.DockerSandbox)
    |         `-- ModelRouterExecutionProvider (job_type="research", wraps trinity_router.ModelRouter)
    |
    +---- Retry / durable backoff (next_run_at — inside execution_worker.py)
    |
    +---- Usage/Budget (src/services/adaptive_budget_service.py — reuses
    |     the EXISTING TCU/entitlement engine, adds no new ledger)
    |
    +---- Sentinel (src/services/sentinel_service.py)
    |
    v
SANDBOX / MODEL ROUTER (both already existed — no new external dependency)
```

## 2. Architecture decisions — what was reused vs. built

Per the mission's explicit instruction not to duplicate existing
capability, here is exactly what already existed and what's new:

| Component | Status | Where |
|---|---|---|
| Docker sandbox (isolation, resource limits, fail-closed) | **Reused, unmodified** | `packages/sandbox/src/trinity_sandbox/docker_sandbox.py` |
| Path-safety / shell blocklist | **Reused, unmodified** | `src/capability/sandbox_utils.py` |
| TCU/entitlement engine (reserve/finalize/cancel, daily/weekly windows) | **Reused, unmodified** | `src/services/entitlement_service.py` |
| 5-hour rolling session window | **Reused, unmodified** | `src/services/session_window_service.py` |
| Startup reconciliation pattern (stale "live" rows -> terminal) | **Pattern reused**, then evolved | `agent_execution_service.py` was the original precedent; Durable Execution's `execution_worker.reclaim_expired_leases()` generalizes it from "unconditionally fail everything live" (only safe for single-process V1) to "reclaim only what a lease proves is orphaned" (safe for any number of processes) |
| Metrics registry | **Reused, unmodified** | `src/observability/metrics.py` |
| `ExecutionJob` lifecycle record | **New** | `src/models/execution.py` |
| `ExecutionProvider` abstraction | **New** | `src/execution/provider.py` |
| Sentinel (verification layer) | **New** — confirmed nothing existed (repo-wide search for "sentinel" was empty before this) | `src/services/sentinel_service.py` |
| Adaptive Budget / capacity tiers | **New**, built entirely on existing usage data | `src/services/adaptive_budget_service.py` |
| `AgentSchedule` (persistent-agent trigger) | **New** — NOT `agent_scheduler.py`, which is concurrency admission control, unrelated; see `models/agent_schedule.py`'s docstring for the explicit disambiguation | `src/models/agent_schedule.py`, `src/services/agent_schedule_service.py` |

**`agent_scheduler.py` was investigated and deliberately NOT touched or
extended** — it is `InMemoryAgentScheduler`, a concurrency admission gate
for synchronous `/agent` HTTP executions (global + per-user slot limits).
It has no time-based triggering concept at all. The mission's "Use o
Scheduler existente" was read as: don't build a second, parallel
CONCURRENCY limiter — which is true, `AgentSchedule`'s firing still goes
through `execution_service.create_job()`, the same single orchestrator an
HTTP request uses. But there was no existing TIME-based dispatcher to
reuse for persistent agents, so `AgentSchedule` + a simple poll loop is
new, minimal (interval-based, not full cron), and explicitly documented as
such rather than silently claimed to be a reuse of something that doesn't
exist.

## 3. Lifecycle

All 10 states the mission originally specified, plus `RETRYING` (added
with Durable Execution — see section 17):

```
CREATED -> QUEUED <-> RETRYING -> STARTING* -> RUNNING -> WAITING -> COMPLETED
                                                    \-> FAILED
                                                    \-> CANCELLED
                                                    \-> EXPIRED
(terminal) -> CLEANUP -> DESTROYED
```

`QUEUED`/`RETRYING -> RUNNING` is the CLAIM transition (atomic —
`execution_worker._try_claim()`); `RUNNING -> QUEUED`/`RETRYING` is a
RELEASE (a recoverable failure, or a reclaimed expired lease). `WAITING`
exists in the state enum for a future Sentinel-driven pause (the current
implementation's retry uses `RETRYING`, not `WAITING`, for the
wait-then-retry step — see `models/execution.py`'s `EXECUTION_STATUSES`).
`CLEANUP`/`DESTROYED` are reserved for a future explicit two-phase
teardown; cleanup (`ExecutionProvider.cleanup_environment()`) runs
synchronously as part of reaching a terminal status, not as its own
visible state — the column values exist so a future phase can split that
out without a schema change. *`STARTING` exists in the status enum for
backward compatibility but the current worker's claim goes straight to
`RUNNING` (claim and "start running" are the same atomic step) — no code
sets `STARTING` today.

Every `ExecutionJob` carries: `id`, `user_id`, `agent_id` (nullable),
`session_id`/`project_id` (nullable), `created_at`/`started_at`/
`completed_at`, `timeout_seconds`, `status`, `error`, `checkpoint`,
`result`, its `artifacts` (via `ExecutionArtifact`, FK), and — added with
Durable Execution — `worker_id`, `lease_expires_at`, `heartbeat_at`,
`next_run_at` (see section 17).

## 4. Async / long-running

`create_job()` persists the row (`status="queued"`) and returns — it never
calls `asyncio.create_task()` or touches asyncio at all. The route
(`POST /execution/jobs`) stays `async def` for FastAPI plumbing reasons
only, not because it dispatches anything. A completely separate durable
worker (`src/services/execution_worker.py`) claims and runs the job,
independent of this or any other HTTP request's lifetime — see "Durable
Execution / Worker Model" below for the full model. Proven in
`tests/integration/test_execution_routes.py::test_R10` — a job's status
is polled via a completely separate HTTP call, without the test ever
dispatching or awaiting anything itself; the app's own background worker
loop (started by the same lifespan a real deploy uses) is what claims and
runs it.

## 5. Checkpoint / resume

```json
{"steps": [{"command": "..."}, {"command": "..."}]}
```
```json
{"completed_step_indices": [0, 1], "step_results": {"0": "...", "1": "..."}}
```
On a retry triggered by a step failure, already-completed step indices are
skipped — a 5-step plan that fails on step 4 resumes at step 4, not step 1
(tested in `test_L03`). On a retry triggered by a **Sentinel** failure the
checkpoint is reset to empty instead: every step already reported success
(that's exactly why Sentinel, not a step, is what failed), so resuming
from "all steps completed" would skip the whole plan and re-check the
same, unchanged workspace forever — tested in `test_L05`.

## 6. Retry / recovery

Retried only when classified recoverable:
- `ExecutionProviderUnavailable` (Docker unreachable at that instant, or a
  lease reclaimed from a presumed-crashed worker — see the Durable
  Execution section)
- a step's `StepOutcome.recoverable=True` (`docker_provider.py`'s
  `_RECOVERABLE_MARKERS` — transient timeout/sandbox-error signals; an
  ordinary non-zero exit from the user's own command is never retried)
- a Sentinel FAIL

`max_retries`/`attempt_number`/`retry_reason` are persisted columns.
Backoff is `EXECUTION_RETRY_BACKOFF_BASE_SECONDS * 2^(attempt-1)` seconds
(default base 5s), applied as a **durable** `next_run_at` timestamp on the
job row — not an in-process `asyncio.sleep()` — so a retry survives the
worker that scheduled it dying before the wait would have ended. A
permanent failure (ordinary command failure, cancellation, capacity
refusal) ends the job once, no retry loop entered.

## 7. Sentinel

`sentinel_criteria` on the job (optional — a job created without it skips
verification entirely) can check: `require_success` (default true),
`expected_artifacts` (real filesystem check, path-traversal-safe — rejects
any path resolving outside the job's own workspace), `min_output_length`,
`forbidden_patterns` (case-insensitive substring match). "Process exited 0"
is never automatically "task complete" when criteria are supplied.

## 8. Usage / Budget — Adaptive Consumption

**No new ledger, no new reservation mechanism.** `adaptive_budget_service.py`
only READS from the existing `UsageRecord`/`EntitlementSession` tables via
`entitlement_service.get_user_period_usage()` and
`session_window_service.get_session_status()`.

Tiers (mission section 10's table, applied literally):

| Capacity remaining | Tier | Admitted priorities |
|---|---|---|
| 100-76% | `normal` | all |
| 75-51% | `normal_optimization` | all (optimization is a signal, not a block) |
| 50-26% | `reduce_non_critical` | critical, high, normal |
| 25-11% | `priority_only` | critical, high |
| 10-1% | `pause_low_priority` | critical only |
| 0% | `pause_execution` | critical only (never fully zero — a critical/safety job must still be able to run) |

`capacity_remaining_pct = min(session_remaining, weekly_remaining)` — the
more constrained dimension, not an average (see
`test_adaptive_budget_service.py::test_B06`).

### The "Sessão atual %" design choice — read this before changing it

This started as a heuristic and has since become a real number; the history
is worth keeping because the shape of the code still reflects it.

Originally `PlanEntitlement` (`src/entitlements.py`) had no session-scoped
TCU field — only daily/weekly/monthly — while the UX wanted a "Sessão
atual: 62% usado" figure. Rather than invent a plan field with no product
decision behind it, `compute_capacity()` measured session "% used" against
`daily_tcu_limit`: a presentation heuristic combining two already-real
numbers.

Product then defined the quota for real — first for MAX and TEAM, and then
for every paid plan in the 2026-09-11 economics pass. So
`session_ceiling = session_tcu_limit or daily_tcu_limit`, and for every
paid plan that resolves to the ceiling actually enforced. The `or` branch
survives for FREE, which has no session budget of its own; a plan with
neither would report `session.pct_used: null`, but none exists.

## 9. User-facing usage — `GET /users/me/capacity`

```json
{
  "tier": "normal",
  "capacity_remaining_pct": 62.0,
  "session": {"active": true, "pct_used": 38.0, "resets_at": "2026-09-03T19:37:22+00:00"},
  "weekly": {"pct_used": 38.0, "resets_at": "2026-09-06T13:00:00+00:00"},
  "active_agents": 2,
  "optimizing": false
}
```

Never contains TCU, tokens, cost, or any internal unit — enforced by
`schemas/execution.py::CapacityResponse`'s field set and tested explicitly
in `test_execution_routes.py::test_R08` (asserts `"tcu"` does not appear
anywhere in the raw response body, case-insensitive). `GET /users/me/usage`
(the pre-existing TCU-denominated endpoint) is untouched — it remains
available for any internal/admin surface that still needs raw figures.

The web app's account page (`packages/web/src/app/(app)/account/page.tsx`)
was rewired to this endpoint, replacing its previous "X / Y TCU (Z%)"
caption — that caption was a real, live violation of this same rule,
predating this mission; fixed as part of this work since it's the exact
surface this mission's section 11 governs. `plans/page.tsx`'s one sentence
literally stating "TCU... é a unidade de consumo do sistema" was also
removed for the same reason. The plan-comparison cards' own quota labels
("10 TCU/dia") were deliberately left alone — redesigning how plan tiers
are marketed/compared is a separate product/pricing decision this mission
does not extend to; only the runtime-consumption-facing text was in scope.

## 10. Agent Budget (per-agent priority)

`AgentDefinition` gained two columns: `priority` (default `"normal"`) and
`execution_timeout_seconds` (default `300`) — internal only, never shown
to the user as a number. A job created with `agent_id` set but no explicit
`priority` override could read the agent's own priority in a future
iteration; V1's `create_job()` takes `priority` as an explicit caller
argument (defaults to `"normal"`) — the agent-level field exists and is
migrated, ready for that wiring, but `create_job()` does not yet
auto-derive priority from `agent_id` (see "Known limitations").

## 11. Persistent agents — `AgentSchedule`

WAKE -> CHECK -> DECIDE -> ACT -> SLEEP, driven by `interval_seconds`
(60s–7 days). A single in-process poll loop
(`agent_schedule_service.run_schedule_poll_loop`, started in `main.py`'s
lifespan, stopped gracefully on shutdown) checks every 30s for schedules
whose `next_run_at <= now`, and calls the SAME `execution_service.create_job()`
an HTTP request would — not a second orchestrator. `next_run_at` advances
from `now` (not from the old value) so a schedule that missed a cycle
(process was down) does not "catch up" by firing repeatedly.

A low-priority persistent agent refused by `CapacityRefusedError` is
**skipped, not retried in a hot loop** — exactly the mission's "não
simplesmente matar todos os agentes" principle: the schedule just waits
for its next cycle.

Routes: `POST /execution/schedules`, `GET /execution/schedules`,
`POST /execution/schedules/{id}/deactivate`.

**Not implemented**: cron expressions (interval-only), webhook-triggered
schedules (a small, real extension of this same table — the trigger
source pattern already generalizes), and dependency-chained execution
("run B after A completes") — a materially bigger feature, not attempted
here.

## 12. Security

- **Sandbox boundary**: unchanged from `DockerSandbox`'s existing,
  already-reviewed contract — `--network=none`, `--read-only` rootfs,
  `--cap-drop=ALL`, non-root user, memory/CPU/PIDs limits, `--rm`
  auto-cleanup. This runtime adds no new privilege.
- **Path traversal**: `ExecutionArtifact` paths and `SentinelCriteria.expected_artifacts`
  are resolved and checked with `Path.is_relative_to()` against the job's
  own workspace — never trusted as given (mirrors `capability/sandbox_utils.py`'s
  `_sandboxed()` pattern).
- **Workspace isolation**: `DockerExecutionProvider.prepare_environment()`
  scopes every job to `{agent_workspace_root}/executions/{user_id}/{job_id}`
  — two jobs, even for the same user, never share a directory (tested in
  `test_L09`).
- **Cross-user access**: every job/schedule route enforces
  `(id, user_id)` ownership — 404 (not 403) for another user's resource,
  matching `billing.py`'s established convention (tested in `test_R03`/`test_R04`).
- **Secrets**: none are injected into the sandboxed environment by this
  runtime — `DockerExecutionProvider` passes only the command string and
  workspace mount; no environment variable injection path exists in V1.
- **Fork bombs / process bombs**: bounded by `DockerSandbox`'s existing
  `--pids-limit`, not reimplemented here.
- **SSRF**: no HTTP-fetching tool is wired into `job_type="shell"` — a
  command that curls an internal address is bounded the same way any
  shell command already is inside the sandbox (`--network=none` by
  default blocks it entirely at the container level).

## 13. Observability

Every state transition and retry logs through the standard `logging`
module (`trinity.execution`, `trinity.execution.worker`,
`trinity.agent_schedule` loggers) and records into the existing
`core_metrics` registry — same registry `/health/metrics` already
exposes, no second metrics system introduced:
`execution_jobs_created_total{priority}` (execution_service.py),
`execution_jobs_completed_total{status}`,
`execution_jobs_retried_total{reason}`,
`execution_jobs_claimed_total`,
`execution_lease_reclaimed_total` (execution_worker.py — new with
Durable Execution: queued/running counts are derivable from
`GET /execution/jobs?status=` today; a dedicated gauge was not added since
nothing yet reads one), `agent_schedule_fired_total`,
`agent_schedule_skipped_total{reason}`. Correlation IDs (`request_id`)
are preserved end-to-end: HTTP routes already inject one via
`RequestLoggingMiddleware`; the worker's own background logs (no HTTP
request driving them) carry `job_id`/`worker_id`/`attempt_number`
instead, which is the durable worker's equivalent correlation key.

## 14. Known limitations (real, not hidden)

- **Durable, but deployed as one process today.** Jobs now survive a
  process restart or worker crash (see section 17) — that gap is closed.
  What remains true: only ONE worker process is actually deployed
  (`railway.toml` — one service). The claim mechanism is DB-level atomic
  and already correct for multiple concurrent worker processes/replicas
  (`test_L16` proves the compare-and-swap claim), so scaling to more than
  one is a deployment-config change, not a code change — but nobody has
  turned that on, and there is no leader election or per-worker health
  dashboard because nothing here needs one (a dead worker's leases simply
  expire and get reclaimed).
- **Heartbeat granularity is per-step, not continuous.** A single step
  that genuinely runs longer than its own `timeout_seconds` +
  `EXECUTION_LEASE_GRACE_SECONDS` could, in principle, have its lease
  expire while still legitimately in progress, causing a second worker to
  claim it. In practice `execute_step()`'s own `asyncio.wait_for(...,
  timeout=timeout_seconds)` guarantees a step can never actually run
  longer than its timeout, and the lease is deliberately granted longer
  than that timeout (see section 17) — so this is a theoretical edge case
  guarded against by construction, not an open race, but it is worth
  naming: a sub-step heartbeat was deliberately not built (real added
  complexity for no correctness benefit given the guarantee above).
- **At-least-once, not exactly-once, for the real external LLM call.** A
  crash in the narrow window between the Model Router call returning and
  its checkpoint commit can cause a duplicate real API call on retry — the
  INTERNAL TCU ledger never double-counts (see section 17's billing-safety
  writeup), but the external provider could genuinely be called twice in
  that specific timing window. No two-phase commit exists to close this;
  it would require idempotency-key support from the LLM providers
  themselves, which none currently offer for completions.
- **No object storage for artifacts.** `ExecutionArtifact` references a
  path in the same local workspace filesystem `agent_service.py`/
  `code_service.py` already use — there is no S3/GCS/Azure Blob client
  anywhere in this codebase to integrate with; inventing one would be
  exactly the kind of premature infrastructure the mission's own "não
  invente uma API externa" rule forbids.
- **`job_type` support: `shell` and `research`.** `research` was added
  after V1 shipped — see section 16. Any other `job_type` still needs its
  own `ExecutionProvider` implementation; the orchestrator itself never
  branches on `job_type` (see models/execution.py's own docstring).
- **Windows dev machine, Linux prod.** `DockerSandbox` already handles
  this (talks to `docker` CLI either way) — verified via the EXISTING,
  already-reviewed sandbox, not re-verified here. This runtime adds no
  new OS-specific assumption.
- **Cron/webhook/dependency-based schedule triggers** — see section 11.
- **Agent-level priority not yet auto-applied** to jobs created for that
  agent — see section 10.
- **Sentinel content validation** (e.g. "is this JSON valid") is not
  implemented — only existence/length/pattern checks, deliberately generic
  across job types.

## 15. Files

**New**: `src/models/execution.py`, `src/models/agent_schedule.py`,
`src/execution/__init__.py`, `src/execution/provider.py`,
`src/execution/docker_provider.py`, `src/execution/fake_provider.py`,
`src/services/execution_service.py`, `src/services/sentinel_service.py`,
`src/services/adaptive_budget_service.py`, `src/services/agent_schedule_service.py`,
`src/schemas/execution.py`, `src/api/routes/execution.py`,
`alembic/versions/0018_execution_runtime_v1.py`, this file.

**Modified**: `src/models/__init__.py`, `src/models/agent_definition.py`
(added `priority`/`execution_timeout_seconds`), `src/main.py` (router
registration, startup reconciliation, poll-loop lifecycle),
`src/api/routes/users.py` (`GET /users/me/capacity`),
`packages/web/src/types.ts`, `packages/web/src/api/client.ts`,
`packages/web/src/app/(app)/account/page.tsx` (TCU removed from the usage
card, rewired to the new capacity endpoint),
`packages/web/src/app/(app)/plans/page.tsx` (one sentence presenting TCU
as "the consumption unit" removed).

Tests: `tests/unit/test_execution_provider.py`,
`tests/unit/test_sentinel_service.py`,
`tests/unit/test_adaptive_budget_service.py`,
`tests/unit/test_agent_schedule_service.py`,
`tests/integration/test_execution_lifecycle.py`,
`tests/integration/test_execution_routes.py`.

## 16. `job_type="research"` — the first real LLM job (added post-V1)

The first job type whose steps actually invoke the EXISTING Model Router
(`trinity_router.ModelRouter`) instead of a sandboxed shell command.
Deliberately implemented as a NEW `ExecutionProvider`
(`src/execution/model_router_provider.py::ModelRouterExecutionProvider`),
not a `job_type` branch inside `execution_service._run_job()`'s step loop —
that loop is completely unaware research jobs exist; it still just calls
`execute_step(workspace, command, timeout_seconds)` and interprets a
generic `StepOutcome`. `command` is the research prompt for this provider
(the ABC's parameter is provider-agnostic by design). Payload shape is
identical to `job_type="shell"`: `{"steps": [{"command": "..."}]}`.

Dispatch: `execution_service.create_job()` resolves
`build_research_provider(...)` (constructed with the job's own
`user_id`/`plan`/`user_timezone`/`session_id`) whenever `job_type=="research"`
and no explicit `provider` was passed — the ONE place the orchestrator
picks a concrete provider class for a job_type, not a runtime branch inside
the execution loop itself.

Accounting: each `execute_step()` call is exactly one
`check_and_reserve()` → `ModelRouter.complete()` → `finalize_usage()` (or
`cancel_reservation()` on any failure) sequence — the SAME entitlement
functions `/chat` and `/agent` already use, imported unmodified. No new
ledger, no double accounting. `ExecutionJob.reservation_id` is kept in
sync generically: after each step, `_run_job()` copies
`getattr(active_provider, "last_reservation_id", None)` onto the job row —
an optional attribute `DockerExecutionProvider` simply doesn't have, so
`job_type="shell"` is untouched.

Capability / provider selection: uses `TaskType.CHAT` and
`capability=Capability.RESEARCH.value`, so `selector.select_provider_chain()`
applies the exact same plan-based chain chat/agent requests get, and the
Etapa 5H `disabled_provider_keys` kill switch is respected per call. The
Etapa 5H *capability* kill switch (`DISABLED_CAPABILITIES` containing
`"research"`) is also checked in `create_job()` before anything is
persisted (`execution_service.CapabilityDisabledError`, mapped to the same
503 `_reject_if_capability_disabled()` already returns for `/chat`/`/agent`)
— otherwise an operator disabling Capability.RESEARCH could be bypassed by
routing through the Execution Runtime instead.

Files: `src/execution/model_router_provider.py` (new).
`src/services/execution_service.py` (job_type accepted set widened to
`("shell", "research")`; `build_research_provider()`; capability-disabled
guard in `create_job()`; `reservation_id` propagation in `_run_job()`'s
step loop — no other change to that loop).
`src/api/routes/execution.py` (`CapabilityDisabledError` → 503).
Tests: `tests/integration/test_execution_research_job.py` (13 tests —
happy path, provider failure/retry, entitlement-exceeded permanent
failure, no-dangling-reservation, checkpoint/resume without re-billing a
completed step, Sentinel fail+retry, Sentinel pass via the provider's own
artifact, cancellation mid-call, capability kill switch, cross-user
isolation, `job_type="shell"` regression, a full HTTP
auth+dispatch+polling E2E test with a faked Model Router transport, and
(added with Durable Execution — see section 17) a dedicated lease-reclaim
billing-safety test simulating a worker crash strictly inside the Model
Router call).

Not done here (same "real gap, not hidden" policy as section 14): no
object-storage for the `research_result.md` artifact (same limitation as
every other artifact — see section 14); no streaming variant (`research`
always uses `ModelRouter.complete()`, never `.stream()` — a job polled over
HTTP has no open connection to stream chunks to).

## 17. Durable Execution / Worker Model

Everything above this section described Execution Runtime V1's original,
in-process/single-worker dispatch. This section describes what replaced
it: `create_job()` now only ever persists a `status="queued"` row (see
section 4); a completely separate durable worker
(`src/services/execution_worker.py`) claims and runs jobs, and jobs
survive a process restart, a worker crash, or (architecturally, though not
deployed today — see "Worker deployment model" below) multiple concurrent
worker processes.

### Why not Redis/Celery/an external queue

Evaluated first, per the mission's explicit instruction. Postgres — the
production database here (Supabase/Railway) — is sufficient. Claiming a
job is a single atomic statement:

```sql
UPDATE execution_jobs
SET status='running', worker_id=:wid, lease_expires_at=:exp, heartbeat_at=:now,
    started_at=COALESCE(started_at, :now), attempt_number=attempt_number+1
WHERE id=:id AND status IN ('queued', 'retrying')
```

The WHERE clause's status check and the SET happen atomically as one
statement in every SQL engine this runs against, SQLite (tests) included
— that's why claiming needs no dialect-specific code path. Two concurrent
workers racing this SAME statement against the SAME row: only one can
ever see `rowcount == 1`; the other sees `0` and moves on to its next
candidate (`tests/integration/test_execution_lifecycle.py::test_L16`).
This is deliberately **not** `SELECT ... FOR UPDATE SKIP LOCKED` — that
construct optimizes avoiding lock contention while a worker holds a row
lock during a multi-statement transaction scanning many candidates; a
bare single-statement UPDATE never holds a lock across statements at all,
so there is nothing for a second worker to block on or need to "skip". It
is the "mecanismo equivalente seguro" the mission explicitly allowed in
place of SKIP LOCKED, with the added benefit of working identically on
SQLite and Postgres.

### Queue

A job is claimable when `status IN ('queued', 'retrying')` AND
(`next_run_at IS NULL OR next_run_at <= now`). `retrying` is a status
distinct from `queued` purely for observability (a fresh job vs. one that
already failed at least once and is waiting out its backoff) — the claim
query treats both identically. Each poll cycle
(`execution_worker.run_worker_once()`) fetches a small batch of
candidates (`EXECUTION_WORKER_BATCH_SIZE`, default 5 — bounds ONE
worker's own execution concurrency, independent of and not a duplicate of
Request Control/Agent Scheduler, which gate HTTP admission, not
background job execution) ranked by priority, then claims and runs them
concurrently (`test_L19`).

### Lease / heartbeat

`lease_expires_at` is granted on claim as
`now + max(EXECUTION_LEASE_DURATION_SECONDS, job.timeout_seconds + EXECUTION_LEASE_GRACE_SECONDS)`
— deriving from the job's OWN configured per-step timeout (not a second,
disconnected duration setting) guarantees a legitimately still-running
step can never outlive its own lease and get pre-empted by the recovery
sweep. The lease is renewed (same formula, recomputed from "now") after
each step completes; `heartbeat_at` is updated at the same points. This
is deliberately coarse — once per step, not continuously mid-step: a
step's own `timeout_seconds` already bounds its maximum duration, so a
sub-step heartbeat would add real complexity (a concurrent heartbeat task
per job) for no correctness benefit here. Defaults:
`EXECUTION_LEASE_DURATION_SECONDS=120`, `EXECUTION_LEASE_GRACE_SECONDS=30`
— both configurable via environment (`src/config.py`).

### Recovery

`execution_worker.reclaim_expired_leases()` finds every `status='running'`
job whose `lease_expires_at` is NULL or in the past — both mean "no live
worker can possibly still own this" (NULL is reachable only via a
malformed/pre-migration row; a genuine claim always sets it). It runs
once at startup (`main.py`'s `_run_execution_reconciliation()`) and
periodically thereafter (`EXECUTION_RECOVERY_INTERVAL_SECONDS`, default
30s — `execution_worker.run_recovery_loop()`). **Startup no longer means
"mark every live job failed"**: V1's `reconcile_stale_jobs_at_startup()`
did that unconditionally, correct only because V1 guaranteed
single-process/in-memory dispatch (no other process could possibly still
own a job). That assumption no longer holds — a "running" job's lease may
still be valid, meaning another worker (another process, another replica,
or simply a poll cycle that hasn't reached its own recovery sweep yet)
may genuinely still be executing it. The universally-safe check is always
"has THIS SPECIFIC lease expired", never "is a process just now starting
up" (`test_L11`, `test_L17`, `test_L18`).

### Billing safety — the one real gap a crash can open, and how it's closed

A worker can crash strictly between `check_and_reserve()` succeeding (a
real `pending` `UsageRecord` already committed) and `finalize_usage()`
running — inside `ModelRouterExecutionProvider.execute_step()`'s Model
Router call. Two things had to change to make this safe:

1. **`ExecutionJob.reservation_id` is written EAGERLY** — immediately
   after the reservation is made, BEFORE the (possibly slow, possibly
   never-returning-if-the-worker-crashes) Model Router call — not lazily
   after `execute_step()` returns (`model_router_provider.py`'s
   `_point_job_at_reservation()`). Without this, the job row would have
   no way to point at the orphaned reservation if the crash happens
   inside the call itself — exactly the window that matters.
2. **`reclaim_expired_leases()` cancels a dangling `pending` reservation**
   before releasing the job for retry, using that pointer.

Without both, a crashed attempt's `pending` reservation would sit forever
alongside the retry's own fresh, successfully-finalized one —
`_sum_active_tcu()` (the quota check inside `check_and_reserve()`) sums
`pending` and `completed` records together specifically to prevent
concurrent over-allocation, so an orphaned `pending` row would silently
and permanently reduce the user's real remaining quota. Verified end to
end in `tests/integration/test_execution_research_job.py::test_R13` —
a real `check_and_reserve()` call, a router call that hangs, a forced
lease expiry, a reclaim, and a real retry — asserting the final ledger
has exactly one `completed` record and one `failed` (cancelled, never
left `pending`) record, never two `completed` records for one job.

### Idempotency / delivery semantics — documented, not oversold

Job delivery is **at-least-once**: a worker crash between a step's real
side effect succeeding (e.g. the LLM call actually returned) and that
step's checkpoint commit means the step is re-executed by whoever
reclaims the job next (`test_L17` — crash before any checkpoint;
`test_L18` — crash after step 0's checkpoint, step 0 is never re-run).
This is not hidden — the mission explicitly permits it ("não prometa
exactly-once"). What IS exactly-once per accepted attempt:
- **Checkpointed steps** — `completed_step_indices` only ever extends
  after a step's own provider call returns successfully, so a
  successfully-checkpointed step is never re-run by a later attempt.
- **The internal TCU ledger** — the billing-safety fix above closes the
  one path that could otherwise double-count internally.

What remains genuinely at-least-once, by design, with no attempt to hide
it: the real external LLM API call itself, in the narrow crash window
between it returning and the checkpoint commit — a real duplicate call to
the actual provider is possible in that exact timing window. No
distributed two-phase commit exists (or is planned) to close that; it
would require the LLM provider itself to support idempotency keys for
completions, which none of the integrated providers do.

### Worker deployment model

Runs as a background `asyncio` task inside the existing FastAPI process
(`main.py`'s `lifespan` — same pattern as `agent_schedule_service`'s poll
loop, started/stopped alongside it). Deliberately **not** a separate
Railway service/process for this iteration: `railway.toml` deploys
exactly one process today, and every claim/lease mechanism in
`execution_worker.py` is DB-level atomic, not in-memory — it is already
fully correct if a separate worker process (or multiple replicas of this
same process) is introduced later; no code here would need to change,
only `railway.toml` would gain a second `[deploy]` block / service.
Splitting today would be additional deployment complexity for zero
additional correctness — the mission's own "menor solução real"
instruction argues against it. Graceful shutdown: on `SIGTERM`/lifespan
exit, both the worker and recovery loops are signalled to stop and given
a bounded grace period (10s) to finish their current poll cycle; a job
still genuinely running past that grace period is simply abandoned — its
lease expires naturally and a future worker (this process's next boot, or
another replica) reclaims it via `reclaim_expired_leases()`. That is the
durability guarantee working as designed, not a gap being papered over.

### New `ExecutionJob` columns (migration `0019_durable_execution`)

`worker_id` (who currently owns this job), `lease_expires_at` (claim
deadline), `heartbeat_at` (per-step liveness marker), `next_run_at`
(durable retry-backoff scheduling). All nullable, no data migration
needed — every pre-existing row gets NULL, which is exactly the correct
"not currently claimed" state. Status enum gained `retrying`. See
`alembic/versions/0019_durable_execution.py`'s own docstring for the full
upgrade/downgrade detail (both tested against a real SQLite DB, including
a from-scratch full-chain upgrade and a clean downgrade).

### Files (Durable Execution)

**New**: `src/services/execution_worker.py`,
`alembic/versions/0019_durable_execution.py`.
**Modified**: `src/models/execution.py` (new columns, `retrying` status),
`src/services/execution_service.py` (`create_job()` no longer dispatches;
`_PROVIDER_OVERRIDES` test hook; `cancel_job()`'s lazy import of
`execution_worker`; `reconcile_stale_jobs_at_startup()` removed, replaced
by `execution_worker.reclaim_expired_leases()`),
`src/execution/model_router_provider.py` (eager `reservation_id` write),
`src/config.py` (`EXECUTION_WORKER_*`/`EXECUTION_LEASE_*`/
`EXECUTION_RECOVERY_*`/`EXECUTION_RETRY_BACKOFF_BASE_SECONDS` settings),
`src/main.py` (worker + recovery loops started/stopped in `lifespan`;
startup reconciliation now calls `reclaim_expired_leases()`).
**Tests**: `tests/integration/test_execution_lifecycle.py` (rewritten —
`L01`-`L15` re-verified against the durable worker, `L16`-`L19` new:
two-workers-race, crash-before-checkpoint, crash-after-checkpoint, batch
size), `tests/integration/test_execution_routes.py` (rewritten — `R01`,
`R09`, `R10` updated for the durable dispatch shape; `R11` new,
confirming the new columns never leak to the client),
`tests/integration/test_execution_research_job.py` (`R01`-`R12` updated
for the durable worker; `R13` new — the billing-safety crash test above),
`tests/unit/test_agent_schedule_service.py` (`AS03` updated — asserts the
fired job is `queued`, not executed directly), `tests/conftest.py`
(`run_worker_until_terminal` fixture — the durable-worker equivalent of
V1's `_wait_for_job()`).

## 18. Persistent Agent Runtime V1 (Tier 1) — WAKE / CHECK / DECIDE / ACT / SLEEP

Closes the real gap an architectural audit found in section 11's original
`AgentSchedule`: despite its own docstring naming the full WAKE → CHECK →
DECIDE → ACT → SLEEP cycle, the implementation only ever did WAKE → ACT —
`job_payload` was forwarded verbatim, unconditionally, on every fire.
CHECK and DECIDE did not exist. This phase (Tier 1) closes that gap with
CHECK and DECIDE both **exclusively non-LLM** — a Tier 2 (an LLM-driven
DECIDE, its own `ExecutionJob`, a two-phase scheduler state machine) was
scoped out by the same audit and remains unbuilt, deliberately.

### The cycle, one poll iteration per schedule

```
WAKE   — schedule.next_run_at <= now. next_run_at is advanced to
         now + interval_seconds IMMEDIATELY, before CHECK/DECIDE/ACT —
         the existing "absolute clock, never extended" rule is completely
         unchanged by this phase.
CHECK  — CheckSource.check(schedule) (src/services/agent_check_source.py).
         Pure, non-LLM, cannot reserve TCU — no import in that module
         reaches trinity_router or entitlement_service. Result
         (CheckResult: changed/signature/reason) is persisted onto
         schedule.last_check_signature EVERY cycle, regardless of outcome.
DECIDE — adaptive_budget_service.should_skip_llm(has_state_changed=...).
         Also pure, non-LLM. No change -> last_decision="skipped", SLEEP.
CONCURRENCY GATE — only reached once DECIDE says "proceed": at most one
         ACTIVE (non-terminal) ExecutionJob per schedule (see below). If
         the schedule's last job is still queued/running/retrying,
         ACT is skipped this cycle too (last_decision="skipped").
ACT    — execution_service.create_job() — the SAME single orchestrator
         entry point the HTTP route uses; nothing else ever creates a job.
         Priority/timeout now resolved from AgentDefinition (see below).
SLEEP  — last_decision/last_decision_at persisted on every branch.
```

### CheckSource — a small, deliberately narrow abstraction

`src/services/agent_check_source.py` defines exactly one abstract method,
`CheckSource.check(schedule) -> CheckResult`. The only implementation
built in this phase, `PayloadSignatureCheckSource`, signs
`schedule.job_payload` (sorted-key JSON -> SHA-256) and compares it to
`schedule.last_check_signature` — the one state source that already
existed on the row, per the mission's explicit "não criar um framework
genérico exagerado" instruction. It detects "the schedule's own task
definition changed since the last wake", not anything about the outside
world — no external fetch mechanism (API/URL/file/event/metric) exists
yet. Adding one is a single new class implementing the same interface;
`_fire_due_schedules()` would not change.

### Concurrency: at most one active job per schedule

`_schedule_has_active_job()` reuses the ALREADY-EXISTING
`AgentSchedule.last_run_job_id` pointer — no new column, no new query
shape, no distributed lock. If that job's status is not in
`EXECUTION_TERMINAL_STATUSES`, a new ACT is refused this cycle. This is a
plain per-schedule DB read, safe under the same reasoning
`execution_worker`'s own atomic claim already established: it's a
correctness gate evaluated fresh every cycle, not a held lock — sufficient
at this phase's scale (single in-process poll loop; see section 4's
worker-deployment note, which applies identically here).

### AgentDefinition defaults now actually used

`AgentDefinition.priority`/`execution_timeout_seconds` existed since
migration 0018 but were dead columns — nothing read them.
`_fire_due_schedules()` now resolves:
- `priority = schedule.priority if schedule.priority is not None else agent.priority`
  — `AgentSchedule.priority` changed from NOT NULL (default `'low'`) to
  nullable with no default (migration `0020`), specifically so "no
  override" becomes expressible and distinct from an explicit `'low'`
  choice. An existing schedule's already-explicit priority is completely
  unaffected; only a NEW schedule created without one now inherits the
  agent's setting instead of silently getting `'low'`.
- `timeout_seconds = agent.execution_timeout_seconds`, always —
  `AgentSchedule` has no timeout-override field of its own (none was
  added — out of Tier 1's explicit scope), so there is nothing to
  conditionally prefer over the agent's setting.

### A real bug fixed along the way: `should_skip_llm()`

`adaptive_budget_service.should_skip_llm(has_state_changed)` existed
since Execution Runtime V1 but had **zero callers** until this phase
wired it into DECIDE. Its body (`return has_state_changed`) was the exact
INVERSE of its own name and docstring ("if not changed, skip the model
call" implies returning `not has_state_changed`) — confirmed via its one
existing test, which asserted the inverted behavior as intentional
"passthrough". Fixed (one line) as part of this phase, since integrating
it as-is would have made every persistent agent skip acting exactly when
something changed, and act only when nothing did. Zero regression risk:
grep confirms no other call site existed before this phase.

### New `AgentSchedule` columns (migration `0020_persistent_agent_check_decide`)

`last_check_signature` (String(64), nullable), `last_decision` (String(20),
nullable, CHECK constraint `IN ('acted','skipped','capacity_refused')`),
`last_decision_at` (DateTime, nullable). `priority` changed to nullable
(see above). All additive/widening — no data migration needed, tested
upgrade+downgrade against a full-chain SQLite rebuild (downgrade backfills
NULL priorities to `'low'`, the pre-Tier-1 default, before re-tightening
the column back to NOT NULL).

### Billing/TCU

No new billing surface. CHECK cannot reserve TCU by construction (see
above). DECIDE is a pure function. ACT is the exact same
`execution_service.create_job()` call every other caller uses — no new
code path into `check_and_reserve`/`finalize_usage`/`cancel_reservation`.
Verified directly: `test_AS23_check_never_creates_a_usage_record` asserts
`usage_records` stays empty across a full skip cycle.

### Not built in this phase (Tier 2, explicitly out of scope)

An LLM-driven DECIDE, `pending_decision_job_id`, any new `job_type`, any
new `ExecutionProvider`, adaptive/backoff interval scheduling (the fixed
absolute-clock interval is unchanged), and an HTTP-level test suite for
`/execution/schedules` (a pre-existing gap, not introduced or widened
here). See the architectural audit that preceded this implementation for
the full Tier 2 design if/when it's approved.

### Files (Persistent Agent Runtime V1, Tier 1)

**New**: `src/services/agent_check_source.py`,
`alembic/versions/0020_persistent_agent_check_decide.py`,
`tests/unit/test_agent_check_source.py`.
**Modified**: `src/models/agent_schedule.py` (new columns, nullable
`priority`), `src/services/agent_schedule_service.py` (CHECK/DECIDE/
concurrency-gate/AgentDefinition-defaults wiring in
`_fire_due_schedules()`; `create_schedule()`'s `priority` default changed
to `None`), `src/services/adaptive_budget_service.py` (`should_skip_llm()`
bug fix), `src/schemas/execution.py` (`CreateAgentScheduleRequest.priority`
now `Optional`; `AgentScheduleResponse` gained the three new fields).
**Tests**: `tests/unit/test_agent_schedule_service.py` (`AS09`-`AS23` new
— CHECK/DECIDE branches, persisted fields, capacity-refused-after-change,
the three-status concurrency gate, terminal-job recovery, AgentDefinition
default/override resolution, fixed-interval regression, no-TCU
regression), `tests/unit/test_adaptive_budget_service.py` (`B07` corrected
to the fixed, non-inverted semantics).

## 19. The first real persistent agent — HTTP monitoring

Proves the WAKE → CHECK → DECIDE → ACT → EXECUTE → SENTINEL → SLEEP cycle
with a genuine external signal, not just the schedule's own payload.
"Monitore esta URL a cada N minutos; se o conteúdo mudar, execute a
ação configurada" — deliberately minimal (no browser/Playwright, no
scraping, no generic HTTP-monitoring framework): one bounded GET/HEAD
request, fingerprinted by `(status_code, body sample)`.

```
WAKE   next_run_at due (unchanged mechanism — section 11/18)
  |
CHECK  HttpCheckSource.check() — src/services/agent_check_source.py
  |    one bounded request, no LLM, no TCU (see below)
  |
DECIDE adaptive_budget_service.should_skip_llm() — unchanged, non-LLM
  |
ACT    execution_service.create_job() — unchanged, the ONLY path in
  |    (only if DECIDE says proceed AND no active job for this schedule)
EXECUTE the Durable Execution Worker (execution_worker.py — UNTOUCHED
  |    by this phase) claims and runs the job exactly like any other
SENTINEL sentinel_service.verify() — the EXISTING mechanism, reachable via
  |    the action's own sentinel_criteria; nothing new here either
SLEEP  next_run_at already advanced at WAKE (section 18's absolute-clock
       rule, unchanged)
```

### job_payload shape — CHECK + ACT, chosen by whether `"check"` is present

```json
{
  "check": {"type": "http", "url": "https://example.com", "method": "GET", "timeout_seconds": 10},
  "action": {"job_type": "research", "payload": {"steps": [{"command": "Summarize what changed."}]}}
}
```

`action.job_type`/`action.payload` are passed to `execution_service.create_job()`
**completely unmodified in shape** — no translation layer, so
`create_job()`'s own `_validate_payload()` is the only validation that
ever runs. A schedule with NO `"check"` key (every schedule from before
this phase) is entirely unaffected: the whole `job_payload` IS the
ExecutionJob payload, job_type is `"shell"`, CHECK is
`PayloadSignatureCheckSource` — the exact Tier-1 behavior, byte for byte.

### The first-observation decision (documented per the mission's explicit ask)

`HttpCheckSource` establishes a **baseline** on the very first check for a
schedule (`changed=False`, `reason="baseline_established"`) — it never
fires an action just because a brand-new agent has never looked at its
URL before. This is DIFFERENT from `PayloadSignatureCheckSource`'s own
first-check behavior (`changed=True` — see section 18), and deliberately
so: that source detects "the schedule's own task definition changed",
where "never evaluated" is itself meaningfully new; `HttpCheckSource`
detects "the external world changed", where "I have no prior observation"
cannot be a change by definition. `_fire_due_schedules()` has no opinion
either way — the decision lives entirely inside each `CheckSource`'s own
`changed` computation, exactly the extension point the abstraction was
built for. No conflict with the existing architecture was found; both
semantics coexist unmodified.

### CHECK = zero TCU, structurally

`agent_check_source.py` imports nothing from `trinity_router` or
`entitlement_service` — not just behaviorally avoided, verified by an
AST-level test of the module's own import statements
(`test_H12_module_never_imports_model_router_or_entitlement_service`).
Only `ACT` (`execution_service.create_job()`, unchanged) can ever create
an `ExecutionJob`, and only a `job_type="research"` action (unchanged
`ModelRouterExecutionProvider` — see section 16) ever touches
`UsageRecord`. An HTTP-monitored schedule using `job_type="shell"`
touches `UsageRecord` zero times across its entire lifecycle — verified
end to end in `test_persistent_http_agent_e2e.py`.

### HTTP security (deliberately minimal — see agent_check_source.py)

- Scheme allowlist: only `http`/`https` — `file://`, `data:`,
  `javascript:`, etc. rejected before any network call.
- SSRF guard: the resolved IP (via `socket.getaddrinfo`, off the event
  loop via `asyncio.to_thread`) is rejected if private/loopback/
  link-local/reserved/multicast/unspecified. `HttpCheckSource(allow_private_networks=True)`
  exists ONLY for tests pointing at a local test server — never set by
  `build_check_source_for_schedule()`, the one production call site.
- Finite timeout: `job_payload.check.timeout_seconds`, defaulting to and
  hard-capped by `AGENT_HTTP_CHECK_DEFAULT_TIMEOUT_SECONDS`/
  `AGENT_HTTP_CHECK_MAX_TIMEOUT_SECONDS` — a misconfigured schedule can
  never stall the poll loop indefinitely.
- Bounded memory: the response is streamed and abandoned after
  `AGENT_HTTP_CHECK_MAX_RESPONSE_BYTES` (default 64 KiB) — never fully
  buffered regardless of the real Content-Length.
- A non-2xx HTTP status is NOT a check failure — it's folded into the
  signature (a site going 200 -> 500 is itself a real, detectable change).
  A genuine failure to observe anything (timeout, connection refused, DNS
  failure, invalid URL) leaves `last_check_signature` untouched rather
  than overwriting a known-good baseline with an error state.
- Logging never includes response headers (a `Set-Cookie` or similar
  could leak there) — only status code and byte counts are logged.

### async CHECK — a necessary fix, not a style change

`CheckSource.check()` became `async def` in this phase (Tier 1 shipped it
`def` — its only implementation had no I/O). `_fire_due_schedules()` runs
inside the SAME event loop as every HTTP request and the durable worker;
a synchronous blocking HTTP call here would have stalled the entire
process for up to the check's timeout, once per due HTTP-checked
schedule, every poll cycle. Every other I/O in this codebase already goes
through `asyncio`/`asyncio.to_thread()` for exactly this reason — this
applies that same established pattern, it does not invent a new one.
`PayloadSignatureCheckSource.check()` became `async def` too (trivially —
still no internal `await`) so both implementations share one interface.

### Concurrency / recovery / capacity — all unchanged, reused as-is

The at-most-one-active-job-per-schedule invariant (section 18),
`priority_admitted()` capacity gating, `AgentDefinition` default
resolution, and `execution_worker.py`'s own lease/heartbeat/reclaim
recovery are **completely untouched** by this phase — an HTTP-monitored
schedule's ACT job is, from the worker's point of view, indistinguishable
from any other `ExecutionJob`. No new recovery mechanism was built or
needed: if the process dies, the schedule row (with its
`last_check_signature`) and any in-flight ACT job both survive exactly as
section 18 and section 17 already describe.

### Files (first real persistent agent — HTTP monitoring)

**New**: `tests/unit/test_agent_http_check_source.py`,
`tests/integration/test_persistent_http_agent_e2e.py`.
**Modified**: `src/services/agent_check_source.py` (`CheckSource.check()`
now `async def`; `HttpCheckSource` + `InvalidCheckUrl` + `_validate_url()`
+ `build_check_source_for_schedule()` added),
`src/services/agent_schedule_service.py` (`_fire_due_schedules()` now
`async def`; per-schedule CheckSource resolution; `_resolve_act_spec()`
for the check+action `job_payload` shape; one new metric,
`agent_schedule_check_result_total{result}`), `src/config.py`
(`AGENT_HTTP_CHECK_*` settings), `tests/unit/test_agent_check_source.py`
(existing tests updated for the async interface).

**Not built in this phase** (explicitly out of scope, not defects):
Tier 2 (LLM-driven DECIDE), a browser/Playwright-based check, response
parsing/scraping beyond a raw byte fingerprint, a generic HTTP-monitoring
framework (headers, auth, cookies, redirect policy beyond httpx's
default), adaptive/backoff scheduling intervals.
