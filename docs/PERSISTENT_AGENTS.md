# Persistent Agents — AgentDefinition, AgentInstance, AgentSchedule, ExecutionJob

This document explains the four-layer chain that makes up a TRINDATRON
persistent agent, what `AgentInstance` (Persistent Agent Instance V1)
adds on top of the WAKE → CHECK → DECIDE → ACT → SLEEP cycle already
documented in `docs/EXECUTION_RUNTIME.md` sections 11, 18, and 19, and
(§14 below) Tier 2 — an LLM-driven DECIDE, replacing Tier 1's non-LLM
`should_skip_llm()` pre-filter with a real, structured, billed decision
for schedules that opt in. Read `EXECUTION_RUNTIME.md` first for the
mechanics of the base cycle — this document does not repeat them.

## 1. The chain

```
User -> AgentDefinition -> AgentInstance -> AgentSchedule -> ExecutionJob -> Worker -> Result/Artifact -> Sentinel
```

| Layer | What it is | Owns state? | Executes anything? |
|---|---|---|---|
| **AgentDefinition** | A reusable template — name, system instructions, skills, model preference, default priority/timeout. | No lifecycle state. | No. |
| **AgentInstance** | A live, running agent — one specific, addressable "thing" a user created from a definition and can pause/resume/inspect. | Yes: `status`, `failure_count`, `last_error`, `last_run_at`, `last_execution_job_id`. | No — never calls `execution_service.create_job()`'s underlying provider directly. |
| **AgentSchedule** | The trigger: an interval, a `job_payload` (CHECK config + ACT config), and the WAKE/CHECK/DECIDE bookkeeping columns (`last_check_signature`, `last_decision`, `last_decision_at`). | Yes: per-cycle CHECK/DECIDE state. | No — DECIDE only chooses whether to call `execution_service.create_job()`. |
| **ExecutionJob** | One concrete unit of work, run by the Durable Execution Worker. | Yes: full durable-execution state machine (see `EXECUTION_RUNTIME.md` §17). | Yes — this is the only layer that actually executes. |

**AgentDefinition 1 → N AgentInstance 1 → N AgentSchedule 1 → N ExecutionJob.**
One definition can back many running instances (e.g. "Page Watcher" used
for five different URLs); one instance can have more than one schedule,
though the common case is one. Every row at every layer is scoped to a
single owning `user_id` — cross-layer ownership is enforced at creation
time (`create_agent_instance()` rejects a definition that belongs to
another user; `create_schedule()` rejects an `agent_instance_id` that
belongs to another user or a different definition than `agent_id`).

## 2. Why AgentInstance exists — what it is not

Before this phase, `AgentSchedule` was the closest thing to "the agent" a
user could see or control: pausing meant deactivating a schedule,
"which agent is this" meant reading `AgentSchedule.agent_id` and joining
to `AgentDefinition`, and there was no single row representing "the
agent I created, that has run 40 times and is currently healthy."
`AgentInstance` is that row. It is deliberately **not**:

- a second execution engine — it never touches a provider, the Model
  Router, or a job queue directly; every action still flows through
  `execution_service.create_job()`, the same single entry point the HTTP
  `/execution/jobs` route and `AgentSchedule`'s own ACT step use;
- a copy of `AgentDefinition` — it does not duplicate name/instructions/
  skills/model preference; those are read from the definition it points
  to, not stored twice;
- a second scheduler — fifteen `AgentInstance` rows still share the one
  existing poll loop (`agent_schedule_service.run_schedule_poll_loop()`);
  fifteen agents do not mean fifteen background loops;
- a place secrets or API keys live — nothing on the model or in any
  response ever carries a credential, `reservation_id`, TCU figure,
  token count, or cost.

## 3. Status state machine

`AgentInstance.status` is one of exactly four values, enforced by a DB
`CHECK` constraint (`AGENT_INSTANCE_STATUSES` in
`src/models/agent_instance.py`):

```
active   — schedules bound to this instance are allowed to WAKE/ACT normally.
paused   — the scheduler's WAKE cycle skips this instance's schedules
           entirely (no CHECK, no DECIDE, no ACT) until resumed. Never
           cancels an in-flight ExecutionJob — that remains a separate,
           explicit action via execution_service.cancel_job().
disabled — reserved for a future explicit-disable action; no route sets
           this in V1, but the state machine already accounts for it so
           adding one later needs no new column or migration.
error    — set automatically by health reconciliation once
           failure_count reaches settings.agent_instance_failure_threshold
           (default 3) consecutive terminal-non-completed ExecutionJobs,
           and ONLY if the instance was "active" at the time (never
           overrides an operator's own pause/disable decision).
```

This status is intentionally never conflated with `ExecutionJob.status`
(which has its own much larger state machine — see `EXECUTION_RUNTIME.md`
§3) or with `AgentSchedule.is_active`. A paused instance's schedule rows
are untouched; only the scheduler's read of the *instance* status decides
whether they're allowed to fire.

## 4. How pause/resume interacts with the wake cycle

`agent_schedule_service._fire_due_schedules()` resolves the owning
`AgentInstance` for any schedule that has one (`agent_instance_id` is
nullable — a schedule with no instance behaves exactly as it did before
this phase, unaffected). If the instance's status is not `"active"`, the
**entire** cycle — CHECK, DECIDE, ACT — is skipped for that schedule this
poll, logged as `reason="instance_{status}"`. This is stronger than
gating only ACT: a paused HTTP-monitoring instance makes zero outbound
requests while paused, not just zero `ExecutionJob`s.

Resuming an instance does not fire an immediate job. `AgentSchedule.next_run_at`
kept advancing on its own absolute clock the entire time the instance was
paused (§18's "absolute clock, never extended" rule is untouched); resume
only means the *next already-scheduled* wake is allowed to proceed. If the
monitored content changed while paused, that change is still detected
correctly at the next wake — `last_check_signature` was never touched
during the pause, so the comparison is against the last real observation,
not a stale-but-silently-accepted one.

## 5. Manual run

`POST /agent-instances/{id}/run` resolves the instance's most recently
created active `AgentSchedule` and calls the *exact same*
`agent_schedule_service._resolve_act_spec()` + `execution_service.create_job()`
pair a real WAKE's ACT step uses — there is no second, manual-only
execution path. It deliberately skips CHECK/DECIDE (a manual trigger is
already the user's own decision to act now) but still enforces:

- the same at-most-one-active-job-per-schedule invariant (refused with
  409 if the instance's `last_execution_job_id` still points at a
  non-terminal job);
- the same capacity gate (`execution_service.CapacityRefusedError`
  propagates to a 429, identical to a real WAKE's own refusal);
- the same 404-not-403 ownership convention as every other route here.

## 6. Health — derived, not a second worker

`AgentInstance.failure_count`/`last_error`/`status="error"` are updated by
`agent_instance_service.reconcile_health_from_job()`, a plain function
called lazily by `_fire_due_schedules()` right before a new ACT's job
overwrites `instance.last_execution_job_id` — it reads the *outgoing*
job's terminal outcome exactly once per transition (a job stuck in
`"retrying"` is never itself counted as a failure). There is no second
poller, no health-check worker, no separate cron: the existing WAKE poll
loop is the only thing that ever calls this.

`GET /agent-instances/{id}`'s `health` field (`healthy`/`degraded`/`failed`)
is derived at read time from `status`/`failure_count` — it is never stored
as its own column, so there is nothing to keep in sync.

## 7. `active_agents` — a corrected metric

`GET /users/me/capacity`'s `active_agents` field now counts
`AgentInstance` rows with `status == "active"`
(`agent_instance_service.count_active_instances()`), not currently-running
`ExecutionJob`s as it did before this phase. An instance idling between
wakes still counts; a raw `ExecutionJob` with no owning instance never
counted and still doesn't. This is a deliberate, user-visible semantic
change to the same field name — see `EXECUTION_RUNTIME.md` §9 for the
endpoint's full response shape.

## 8. Legacy compatibility

`AgentSchedule.agent_instance_id` is a new, nullable, additive foreign key
— `AgentSchedule.agent_id` (→ `AgentDefinition`) is completely unchanged,
never renamed or repurposed. Every schedule that existed before this
phase was backfilled (migration `0021_agent_instance`) to point at exactly
one new `AgentInstance` per distinct `(user_id, agent_id)` pair, seeding
`last_run_at`/`last_execution_job_id` from whichever of that pair's
schedules had run most recently — no duplicate executions, no data loss,
verified against a full upgrade/downgrade/backfill cycle.

## 9. HTTP surface

Mounted at `/agent-instances`, deliberately **not** `/agents` — that path
already serves `AgentDefinition` CRUD (`src/api/routes/agent_definitions.py`
and the pre-existing `packages/web/src/app/(app)/agents` page). Reusing it
for a materially different resource would have meant either breaking the
existing API or renaming it — both out of scope. See
`src/api/routes/agent_instances.py`'s own module docstring.

| Route | Purpose |
|---|---|
| `POST /agent-instances` | Create an instance from a definition (+ optional `project_id`). |
| `GET /agent-instances` | List the caller's own instances. |
| `GET /agent-instances/{id}` | Read one instance, including derived `health`/`next_run_at`/`current_execution_job_status`. |
| `PATCH /agent-instances/{id}` | Update `name`/`project_id` only — never `status` (see §3). |
| `POST /agent-instances/{id}/pause` | Idempotent pause. |
| `POST /agent-instances/{id}/resume` | Resume from `paused` only. |
| `POST /agent-instances/{id}/run` | Manual run — see §5. |

None of these responses ever include a secret, `reservation_id`, TCU
figure, token count, or cost — see `schemas/agent_instance.py`'s own
docstring.

## 10. Frontend

`packages/web/src/app/(app)/agent-instances/page.tsx` — the first real
UX for this layer: name, status, owning definition, last/next run,
derived health, pause/resume, and "run now", plus a create modal that
picks an existing `AgentDefinition`. Deliberately **not** built in this
phase: a workflow builder, an agent marketplace, a multi-agent canvas, or
any cross-instance orchestration UI — see the non-goals list below.

## 11. Explicit non-goals of this phase

Not built, not started, deliberately out of scope: Tier 2 (an LLM-driven
DECIDE — see `EXECUTION_RUNTIME.md` §18's own "not built" list),
autonomous reasoning loops, agent-to-agent delegation, vector-backed
memory (the existing flat Memory system is reused unmodified), browser/
social-media agents, scaling validation for a real 15-agent fleet, Redis/
Celery or any external queue, an agent marketplace, and advanced
analytics beyond the five counters in §12.

## 12. Observability

`core_metrics` only has `Counter`/`Histogram` primitives (no `Gauge`
exists anywhere in this codebase) — the requested "created/active/paused/
failed" figures are therefore transition counters, not live gauges:

- `agent_instances_created_total` — once per `create_agent_instance()`.
- `agent_instances_status_transitions_total{to_status}` — fires on every
  transition reaching `active` (create, resume), `paused` (pause), or
  `error` (health reconciliation crossing the failure threshold). Summing
  a label over a window answers "how many instances became X" — the
  counter-only equivalent of a gauge snapshot.
- `agent_instance_manual_runs_total` — once per successful manual run.

No cost/TCU/token metric was added — see §1's "not a place secrets live"
note; the same applies to observability.

## 13. Files

**New**: `src/models/agent_instance.py`,
`src/services/agent_instance_service.py`, `src/schemas/agent_instance.py`,
`src/api/routes/agent_instances.py`,
`alembic/versions/0021_agent_instance.py`,
`tests/unit/test_agent_instance_service.py`,
`tests/integration/test_agent_instance_routes.py`,
`tests/integration/test_persistent_agent_instance_e2e.py`,
`packages/web/src/app/(app)/agent-instances/page.tsx`.
**Modified**: `src/models/agent_schedule.py` (`agent_instance_id`),
`src/models/__init__.py`, `src/services/agent_schedule_service.py`
(instance-status gate + health reconciliation in `_fire_due_schedules()`;
`create_schedule()` gained `agent_instance_id`), `src/api/routes/users.py`
(`active_agents` now `agent_instance_service.count_active_instances()`),
`src/main.py` (router registration), `tests/integration/test_execution_routes.py`
(`R09` rewritten for the corrected `active_agents` semantics),
`packages/web/src/api/client.ts`, `packages/web/src/types.ts`,
`packages/web/src/components/layout/sidebar.tsx`.

## 14. Tier 2 — LLM Decision Runtime

Everything above this section is Tier 1: CHECK and DECIDE are both
exclusively non-LLM (`agent_check_source.py`,
`adaptive_budget_service.should_skip_llm()`). Tier 2 replaces DECIDE with
a real, structured, billed LLM decision for any schedule that opts in —
Tier 1 stays byte-for-byte unchanged for every schedule that doesn't.

### The cycle

```
WAKE   next_run_at due (unchanged mechanism)
  |
CHECK  same non-LLM CheckSource as Tier 1 — cheap, no TCU
  |
  | should_skip_llm(changed) still gates whether an LLM is even asked —
  | "nothing changed" never reaches DECIDE, exactly like Tier 1
  v
DECIDE ExecutionJob created (job_type="agent_decide") — a REAL, durable,
  |    billed job. The scheduler does NOT wait for it: it persists a
  |    queued row and returns to the next schedule in this poll cycle.
  v
[later, possibly several poll cycles later]
EXECUTE the Durable Execution Worker (execution_worker.py — UNTOUCHED)
  |    claims and runs the DECIDE job exactly like any other, through
  |    AgentDecisionExecutionProvider (src/execution/agent_decision_provider.py)
  v
[later, on this schedule's NEXT due wake]
CONSUME the completed DECIDE job — atomic compare-and-swap claim on
  |    AgentSchedule.pending_decision_job_id (see "Crash-safe consumption"
  |    below). decision="skip" or invalid output -> SLEEP, no ACT.
  v
ACT    decision="act" -> execution_service.create_job() — the SAME single
  |    orchestrator entry point every other caller uses. job_type is
  |    ALWAYS "research" (see "Allowed actions" below).
EXECUTE the SAME Durable Execution Worker runs the ACT job
  |
SENTINEL sentinel_service.verify() — the EXISTING mechanism, with a
  |    server-fixed `{"min_output_length": 1}` criteria (never LLM-supplied)
  v
SLEEP  next_run_at already advanced at WAKE
```

The scheduler never blocks on the LLM and never calls the Model Router
directly from the poll loop — DECIDE is always a separate, independently
scheduled `ExecutionJob`, claimed and executed by the same worker that
runs everything else.

### Opting a schedule into Tier 2

`job_payload = {"check": {...}, "decide": {}}` — the presence of `"decide"`
(alongside `"check"`) selects Tier 2 instead of Tier 1's
`{"check": {...}, "action": {...}}`. `create_schedule()` rejects a
`job_payload` containing BOTH `"action"` and `"decide"` — a schedule must
pick exactly one DECIDE strategy. `"decide"`'s value is currently unused
(reserved for future per-schedule overrides, e.g. a different prompt
version) — an empty `{}` is the only supported value today.

### DECIDE is a real ExecutionJob — not a special case

`job_type="agent_decide"` (`src/execution/agent_decision_provider.py`)
implements the same `ExecutionProvider` ABC as `"shell"`/`"research"` —
claimed/leased/retried/recovered by the durable worker with ZERO changes
to `execution_worker.py`. It cannot be created directly via
`POST /execution/jobs` (rejected with 422 — it is internal-only, created
solely by the scheduler's own WAKE cycle, since a standalone DECIDE job
would have no owning schedule to ever consume its result).

### The decision contract (`src/services/agent_decision.py`)

```json
{"decision": "act", "reason": "...", "action": {"job_type": "research", "payload": {"steps": [{"command": "..."}]}}}
```
or
```json
{"decision": "skip", "reason": "..."}
```

`parse_decision()` validates this STRICTLY — malformed JSON, an unknown
`decision` value, or (for `"act"`) a missing/invalid `action` all return
`None`, and `None` is NEVER treated as an implicit `"act"`. The SAME
function is called twice, independently: once inside
`AgentDecisionExecutionProvider.execute_step()` (deciding whether the
DECIDE job's STEP succeeded) and once inside
`agent_schedule_service._consume_pending_decision()` (defense in depth —
never trusting a stored `"completed"` job's result blindly).

### Allowed actions — deliberately narrow

`agent_decision.ALLOWED_DECIDE_ACT_JOB_TYPES = ("research",)`. The model
can NEVER select `"shell"` (or any other job_type) — it cannot generate
or trigger arbitrary shell execution. `user_id`/`agent_id`/`session_id`/
`project_id` for the ACT job are ALWAYS taken from the schedule/instance
(server-controlled) — the model's output has no `user_id`/ownership field
at all, so there is structurally nothing for it to override, request a
different workspace with, or use to reach another user's data.

### Prompt injection boundary

CHECK observes EXTERNAL content (today: an HTTP response body, bounded
and sampled into `CheckResult.observed_excerpt`). That content is placed
ONLY inside the DECIDE user message's `OBSERVED CONTENT` section — never
inside, prepended to, or merged with the SYSTEM message
(`agent_decision.DECIDE_SYSTEM_PROMPT_V1`), which is a versioned,
code-owned constant never read from `job_payload` or any other
persisted/user-adjacent config. The system prompt explicitly instructs
the model to treat observed content as data, never as instructions.

This is a defense-in-depth boundary, not a single point of trust: even if
a model were fooled by injected text into requesting something outside
policy, the STRUCTURAL allowlist (`ALLOWED_DECIDE_ACT_JOB_TYPES`) and
payload-shape validation (`parse_decision()`) still refuse it
mechanically — `test_persistent_agent_decide_e2e.py`'s prompt-injection
test verifies both properties directly: the adversarial text reaches only
the user message, and a decision that asks for a disallowed `job_type`
(as if hijacked) is refused regardless of how the model produced it.

### Crash-safe, at-most-once consumption

`AgentSchedule.pending_decision_job_id` (migration `0022_agent_decide_tier2`)
tracks the outstanding DECIDE job. Consuming a terminal DECIDE job uses
an ATOMIC compare-and-swap `UPDATE ... SET pending_decision_job_id = NULL
WHERE pending_decision_job_id = <that job's id>` — the SAME claim
TECHNIQUE `execution_worker._try_claim()` already uses for job claiming,
reused (not reinvented) one layer up, at the schedule. Only the caller
that wins the CAS (`rowcount == 1`) proceeds to create the ACT job; every
other concurrent, duplicate, or restarted attempt sees `rowcount == 0`
and does nothing. Verified directly
(`test_AS41_crash_safe_consumption_at_most_one_act`,
`test_e2e_two_poll_cycles_consume_same_decide_at_most_once`) and via two
full "process restart" simulations
(`test_e2e_crash_recovery_decide_completed_before_restart`,
`test_e2e_crash_recovery_no_duplicate_act_after_restart`) — a fresh
`Session`/identity map, exactly like `AgentInstance`'s own restart tests.

The one accepted failure mode is FAIL-CLOSED: an exceptionally rare crash
between the CAS commit and a successful `create_job()` call for the ACT
can silently drop an `"act"` decision (zero ACT jobs for that decision —
the next CHECK simply re-evaluates fresh), but can NEVER produce two.
"No máximo um ACT" is the hard requirement; under-delivery in an
astronomically narrow window is the accepted, documented trade-off, not
a hidden gap — consistent with `execution_worker.py`'s own "at-least-once
delivery, never double-bill" philosophy applied to the opposite direction
(here: never double-act).

### Billing — DECIDE and ACT have independent reservations

`AgentDecisionExecutionProvider` reuses the EXACT
`check_and_reserve()` / Model Router call / `finalize_usage()` /
`cancel_reservation()` sequence `ModelRouterExecutionProvider` (the
`"research"` provider) already established — copied deliberately, not
shared via a base class (see that provider's own docstring on the
trade-off). A router-level failure (timeout/unavailable/rejected/
unexpected) cancels the reservation, exactly like `"research"`. A REAL
response that fails `parse_decision()` (malformed JSON) is still
`finalize_usage()`'d — the model was genuinely called and genuinely cost
tokens; only the STEP is marked failed (triggering the existing job-level
retry policy), never the billing. CHECK itself still never bills anything
(same Tier 1 guarantee, structurally — `agent_check_source.py` imports
neither `trinity_router` nor `entitlement_service`). DECIDE and ACT are
always two separate `ExecutionJob` rows with two independent
`reservation_id`s — never combined into one charge. Verified end to end
in `test_e2e_billing_decide_and_act_have_independent_reservations`.

### Capacity

DECIDE creation itself goes through the SAME
`execution_service.create_job()` admission check (capacity tier +
`priority_admitted()`) every job already does — a capacity-refused DECIDE
records `last_decision="capacity_refused"` and moves on; it does NOT
retry-loop creating DECIDE jobs (the schedule simply becomes due again on
its normal interval, and CHECK re-evaluates fresh then). The SAME applies
to a capacity-refused ACT once a decision has already been made.

### AgentInstance status vs. DECIDE failure — kept separate

A DECIDE job failing (bad JSON, provider error, exhausted retries) is
recorded as `AgentSchedule.last_decision="decide_failed"`/`"decide_invalid"`
— it does NOT, by itself, affect `AgentInstance.status`/`failure_count`.
Health reconciliation (`agent_instance_service.reconcile_health_from_job()`)
only ever runs against a completed ACT job's own terminal outcome
(unchanged from Persistent Agent Instance V1) — a DECIDE that failed but
whose schedule later successfully acts is not held against the instance's
health. No new retry engine, no second failure-counting mechanism.

### Observability

`agent_decisions_created_total`, `agent_decisions_act_total`,
`agent_decisions_skip_total`, `agent_decisions_failed_total{reason}`,
`agent_decision_latency_ms` (a histogram: creation-to-consumption
latency) — all in `agent_schedule_service.py`, all counter/histogram-only
(no `Gauge` exists in this codebase's metrics registry — see that
module's own inline note). No cost/TCU/token metric was added.

### Frontend

`GET /agent-instances` gained one additional, optional field:
`last_decision` — the most-recently-decided active schedule's own
`AgentSchedule.last_decision` value (e.g. `"acted"`, `"decide_skip"`),
shown on the existing `/agent-instances` page next to last/next run.
Still never TCU/tokens/reservation/provider detail.

### Explicitly not built in this phase

Agent-to-agent delegation, a real 15-agent fleet load test, vector-backed
memory, browser/social-media agents, an agent marketplace, self-modifying
prompts/agents, and an unrestricted/arbitrary-shell autonomous action path
— all deliberately out of scope, matching Persistent Agent Instance V1's
own non-goals list (§11) plus this phase's own explicit exclusions.

### Files (Tier 2 — LLM Decision Runtime)

**New**: `src/services/agent_decision.py`,
`src/execution/agent_decision_provider.py`,
`alembic/versions/0022_agent_decide_tier2.py`,
`tests/unit/test_agent_decision.py`,
`tests/integration/test_execution_agent_decide_job.py`,
`tests/integration/test_persistent_agent_decide_e2e.py`.
**Modified**: `src/models/agent_schedule.py` (`pending_decision_job_id`,
widened `AGENT_SCHEDULE_DECISIONS`), `src/services/agent_schedule_service.py`
(`_is_llm_decide_schedule()`, `_process_llm_decide_schedule()`,
`_consume_pending_decision()`, wired into `_fire_due_schedules()`;
`create_schedule()` rejects `"action"`+`"decide"` together; five new
metrics), `src/services/agent_check_source.py`
(`CheckResult.observed_excerpt`, populated by `HttpCheckSource`),
`src/services/execution_service.py` (`job_type="agent_decide"` admitted;
`build_decision_provider()`; RESEARCH capability kill switch reused for
it too), `src/services/execution_worker.py` (`_resolve_provider()`
dispatches `"agent_decide"` to the new provider), `src/api/routes/execution.py`
(422 for a directly-submitted `"agent_decide"` job), `src/schemas/agent_instance.py`
+ `src/api/routes/agent_instances.py` (`last_decision` field),
`tests/unit/test_agent_schedule_service.py` (`AS33`-`AS43`),
`packages/web/src/types.ts` + `packages/web/src/app/(app)/agent-instances/page.tsx`
(`last_decision` display).
