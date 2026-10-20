"""Load Classification — Etapa 5C of the TRINITY SCALE ENGINE.

Classifies a request into LoadClass.LIGHT / MEDIUM / HEAVY based on its
OPERATIONAL load (which executor actually runs, with what tool access),
never on content type alone and never on an LLM call. Deterministic, local,
side-effect-free — see LoadClassifier.classify() below.

This is preparatory infrastructure for the future Agent Scheduler (Etapa
5D) and cost/observability policies. As of Etapa 5C it changes NOTHING
about how a request is handled: no queueing, no priority, no change to
Request Control's limits, no change to TCU/billing/Model Router. Callers
(src/api/routes/sessions.py) only LOG the computed LoadClass today — it is
not read by any behavioral code path yet.

── Why TaskType is the primary axis ────────────────────────────────────────
Capability (src/capability/classifier.py) already has a real dispatch
counterpart: get_task_type(capability) → TaskType.CHAT or TaskType.AGENT
(src/capability/pipelines.py) — this is the EXISTING abstraction that
decides which executor a request actually runs on:

  TaskType.CHAT  → chat_service.stream_chat()  — a plain text-completion
                    loop with NO tool access whatsoever, regardless of what
                    capability the content resembles.
  TaskType.AGENT → agent_service.stream_agent() — the full ReAct loop, with
                    access to the ENTIRE tool registry (read_file,
                    write_file, list_directory, execute_shell, web_search,
                    browser_navigate, browser_get_text, browser_screenshot)
                    for up to agent_max_iterations turns, regardless of
                    which AGENT-task-type capability triggered it.

Since the actual operational load (tool access, iteration count, whether a
Docker sandbox or a real Chromium process can be spawned) is determined by
TaskType, not by the finer-grained Capability, TaskType is this module's
primary classification axis. Capability is used only to refine WITHIN the
AGENT bucket, for the one case the product spec calls out explicitly (see
FILES below) — everything else in that bucket defaults to HEAVY.

── The /agent and /chat endpoints force their TaskType ─────────────────────
sessions.py's /chat and /agent endpoints always call stream_chat() /
stream_agent() respectively, regardless of what classify(body.content)
would guess — a plain "hello" sent to /agent still runs the full ReAct
loop. Callers on those two endpoints MUST pass `forced_task_type` (CHAT or
AGENT, matching the endpoint) rather than letting the classifier derive it
from a content-based capability guess — see classify()'s docstring. Only
/message dispatches purely by capability (task_type = get_task_type(
capability) IS how it picks an executor), so it can omit forced_task_type.

── Mapping rationale, capability by capability ──────────────────────────────
  CHAT              → LIGHT   (TaskType.CHAT bucket — see above; no tools)
  IMAGE/VIDEO/AUDIO → LIGHT   (TaskType.CHAT bucket in today's routing —
                                see pipelines.py's CAPABILITY_TASK_TYPE.
                                These are additionally intercepted by
                                UNIMPLEMENTED_CAPABILITIES *before* either
                                executor runs, in /message and in
                                stream_agent(), returning an immediate
                                canned "coming soon" reply — the lightest
                                possible request in the whole system. NOT
                                classified HEAVY just because they are
                                non-textual content types, per explicit
                                product guidance: load reflects what
                                actually executes, not the content type.)
  FILES             → MEDIUM  (TaskType.AGENT bucket — explicit product
                                decision, named directly in the spec.
                                Documented tension: as of today FILES runs
                                through the IDENTICAL full-toolset ReAct
                                loop as every HEAVY AGENT capability — there
                                is no code-level restriction limiting it to
                                just file tools (only a per-agent-definition
                                skill_ids restriction exists, an orthogonal
                                per-session concept, not per-capability).
                                MEDIUM here anticipates a future narrower
                                "file tools only" agent profile; it is an
                                intentional simplification, not a measured
                                fact about today's actual resource usage.)
  RESEARCH, CODE    → HEAVY   (TaskType.AGENT bucket, no explicit product
                                guidance either way — conservatively
                                defaulted to HEAVY per the "prefer the
                                heavier class when ambiguous" instruction,
                                since both have unrestricted access to the
                                full tool registry, same as Capability.AGENT
                                itself.)
  WORK              → HEAVY   (explicitly named "browser automation" in
                                the product spec's HEAVY examples.)
  AUTOMATION        → HEAVY   (workflow/pipeline/webhook — "potentially
                                long-running", an explicit HEAVY criterion.)
  AGENT             → HEAVY   (explicit product requirement.)

Any capability NOT in the AGENT-bucket override dict below (i.e. every one
except FILES) falls through to the bucket's HEAVY default — this is also
the safety net for a hypothetical future Capability value this module
hasn't been updated for yet: unknown-within-the-AGENT-bucket rounds up to
HEAVY, never down to MEDIUM or LIGHT.
"""
from __future__ import annotations

from enum import Enum

from trinity_router import TaskType

from src.capability import Capability, get_task_type


class LoadClass(str, Enum):
    """Coarse operational-load classification for a request. Consumed
    today only for observability (logged, not branched on) — see this
    module's docstring. Ordering LIGHT < MEDIUM < HEAVY is intentional so a
    future consumer can compare classes, but no code relies on that yet.
    """
    LIGHT = "light"
    MEDIUM = "medium"
    HEAVY = "heavy"


# TaskType.CHAT executes stream_chat() — no tool access, ever, regardless
# of capability. A flat constant (not a per-capability dict) because there
# is no capability-level distinction that changes what actually runs here.
_CHAT_TASK_LOAD_CLASS = LoadClass.LIGHT

# TaskType.AGENT executes stream_agent()'s full-toolset ReAct loop for
# every capability except the one explicit override below — see the
# module docstring's "Mapping rationale" for why each entry (and the
# HEAVY default for everything absent from this dict) was chosen.
_AGENT_CAPABILITY_LOAD_CLASS: dict[Capability, LoadClass] = {
    Capability.FILES: LoadClass.MEDIUM,
    # A deck is the heaviest thing the product runs: minutes of wall time,
    # one model call per slide, and a container doing the rendering. HEAVY
    # is also the default for anything absent from this dict, but it is
    # spelled out because "it happened to fall through to the right answer"
    # is not the same as a decision.
    Capability.PRESENTATION: LoadClass.HEAVY,
}
_AGENT_TASK_DEFAULT = LoadClass.HEAVY


class LoadClassifier:
    """Deterministic, local, side-effect-free load classifier.

    No LLM call, no external I/O, no network access, no shared/mutable
    state read or written by classify() — every call is a pure function of
    its arguments. Safe to call concurrently from any number of
    coroutines/threads with no locking (see
    tests/unit/test_load_classification.py's concurrency test).
    """

    def classify(self, capability: Capability, *, forced_task_type: TaskType | None = None) -> LoadClass:
        """Classify by operational load, not content type.

        `forced_task_type`: pass the TaskType the caller KNOWS will
        actually execute (TaskType.CHAT for the /chat endpoint,
        TaskType.AGENT for the /agent endpoint — both force their executor
        unconditionally, independent of what `capability` merely resembles
        from raw text). Omit only when task_type truly follows from
        capability by construction (e.g. /message, whose executor choice
        IS get_task_type(capability)) — in that case it is derived
        automatically, with the exact same result as passing it explicitly.
        """
        task_type = forced_task_type if forced_task_type is not None else get_task_type(capability)
        if task_type == TaskType.CHAT:
            return _CHAT_TASK_LOAD_CLASS
        return _AGENT_CAPABILITY_LOAD_CLASS.get(capability, _AGENT_TASK_DEFAULT)


# Process-wide instance — stateless, so a singleton is purely a convenience
# (unlike request_control.py's singletons, there is no state here that
# would need per-test resetting).
load_classifier = LoadClassifier()
