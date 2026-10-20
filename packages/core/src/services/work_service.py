"""
TRINITY WORK — research and task execution service with approval gates.

This service executes complex, multi-step tasks using web research tools.
For actions that have external effects (send_notification, schedule_action),
an approval gate pauses the stream until the user approves or rejects.

Security design:
- Approval gate uses a per-action UUID known only to the authenticated client.
- Gate times out after _GATE_TIMEOUT seconds to prevent resource leaks.
- No filesystem or shell access — WORK handles research and coordination only.
- Web content is treated as untrusted data; the LLM never acts on it without
  explicit approval for actions with external effects.
- browse_url() validates the RESOLVED IP of every fetch (including each
  redirect hop) against private/loopback/link-local/reserved ranges
  before connecting (see _assert_url_is_safe_to_fetch) — SSRF-01,
  Security Hardening.

SSE event sequence:
  status (preparing) → status (tool events) × N
  → [approval_required] → status (approved|rejected) → ...
  → done | error
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid_module
from typing import AsyncGenerator

from sqlalchemy.orm import Session as DBSession
from trinity_agent.tools.actions import ScheduleAction, SendNotification
from trinity_agent.tools.documents import CreateDocument
from trinity_agent.tools.registry import ToolRegistry
from trinity_agent.tools.search import WebSearch
from trinity_agent.tools.web import BrowseUrl
from trinity_agent.tools.web import UnsafeUrlError as UnsafeUrlError  # noqa: F401 — back-compat re-export, see below
from trinity_agent.tools.web import assert_url_is_safe_to_fetch as _assert_url_is_safe_to_fetch  # noqa: F401 — back-compat re-export, see below
from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    RoutingRequest,
    TaskType,
)

from src.capability.activity_stream import status_event, tool_status_event
from src.config import settings
from src.models.work_workspace import WorkWorkspace
from src.services.approval_gate import PendingGate, create_gate, wait_for_gate
from src.services.approval_gate import resolve_gate as _resolve_gate_impl
from src.services.code_service import _record_partial_usage
from src.prompting.identity_guard import redact_self_identification, safe_error_payload
from src.services.entitlement_service import (
    cancel_reservation,
    check_agent_iteration_budget,
    finalize_usage,
    get_reservation_budget_tcu,
)

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_GATE_TIMEOUT = 300  # 5 minutes to approve/reject before the gate times out
_MAX_OUTPUT_CHARS = 4_000

# Tool/Connector Foundation etapa: UnsafeUrlError and _assert_url_is_safe_to_fetch
# used to be defined HERE — they now live in trinity_agent.tools.web (imported
# above as thin re-exports under their ORIGINAL names) alongside the new
# canonical BrowseUrl tool, so this module's own security regression suite
# (tests/security/test_ssrf_final_gate.py) keeps passing against the exact
# same names/behavior without any test changes.


def _build_work_tool_registry() -> ToolRegistry:
    """The canonical tool set for TRINITY WORK — same infrastructure the
    Agent ReAct loop uses (trinity_agent.tools.registry.ToolRegistry),
    replacing the old hand-rolled _WORK_TOOLS schema list +
    _run_work_tool() dispatcher. web_search is the SAME WebSearch class
    Agent already registers (was previously a byte-for-byte duplicate
    inline implementation here); browse_url/create_document/
    send_notification/schedule_action are new BaseTool subclasses moved
    from this module's own prior inline implementations, unchanged in
    behavior."""
    registry = ToolRegistry()
    registry.register(WebSearch())
    registry.register(BrowseUrl())
    registry.register(CreateDocument())
    registry.register(SendNotification())
    registry.register(ScheduleAction())
    return registry


async def _run_work_tool(name: str, args: dict) -> tuple[bool, str]:
    """Back-compat wrapper — src.services.work_service.tests.security's
    own SSRF regression suite calls this directly by name. Delegates to
    the canonical ToolRegistry; same (bool, str) contract as before.

    Note: _CONFIRM_TOOLS (send_notification, schedule_action) are never
    called here directly — the approval gate in stream_work_task handles
    them and only calls this (indirectly, via the same registry) after
    approval, exactly as before this etapa.
    """
    registry = _build_work_tool_registry()
    result = await registry.call(name, args)
    return result.success, (result.output if result.success else result.error)


WORK_TASK_SYSTEM_PROMPT = (
    "You are TRINITY WORK, the research-and-task-execution mode of TRINDATRON. TRINITY WORK is "
    "a technical component name, not the product name — if asked what product or company this "
    "is, answer TRINDATRON. "
    "You have access to tools for web research, document creation, and external actions. "
    "Guidelines:\n"
    "1. Research thoroughly before drawing conclusions.\n"
    "2. Cite sources when providing factual information.\n"
    "3. External actions (send_notification, schedule_action) require user approval "
    "   and will pause until the user confirms.\n"
    "4. When your task is complete, provide a clear, structured summary.\n"
    "5. Do NOT call any tool once you have finished — respond directly."
)

# ── Approval gate state ───────────────────────────────────────────────────────
# Security etapa (post be6e1b0): the real gate implementation now lives in
# src.services.approval_gate, shared with TRINITY AGENT — see that
# module's own docstring for why. _create_gate/_wait_for_gate/resolve_gate
# below are thin, byte-for-byte-compatible wrappers so workspaces.py's
# route (which calls resolve_gate(..., workspace_id=...) by keyword)
# needs zero changes.

_router = ModelRouter()


def _create_gate(action_id: str, workspace_id: str) -> PendingGate:
    return create_gate(action_id, owner_id=workspace_id)


async def _wait_for_gate(action_id: str, gate: PendingGate) -> bool:
    return await wait_for_gate(action_id, gate, timeout=_GATE_TIMEOUT)


def resolve_gate(action_id: str, *, approved: bool, workspace_id: str) -> bool:
    """Resolve a pending approval gate.

    workspace_id must match the workspace that created the gate — prevents a user
    from resolving another user's gate by guessing or obtaining the action_id.
    Returns True if the gate existed and belonged to this workspace.
    """
    return _resolve_gate_impl(action_id, approved=approved, owner_id=workspace_id)


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


# ── Main stream ───────────────────────────────────────────────────────────────

async def stream_work_task(
    db: DBSession,
    workspace: WorkWorkspace,
    task: str,
    max_iterations: int = 8,
    workspace_id: str | None = None,
    user_plan: str | None = None,
    reservation_id: str | None = None,
    user_id: str | None = None,
    user_timezone: str | None = None,
) -> AsyncGenerator[str, None]:
    """Execute a WORK task via SSE ReAct loop.

    CONFIRM-level tools (send_notification, schedule_action) pause execution
    and emit an approval_required event. The client must call /approve or /reject
    before the stream resumes. A timeout of _GATE_TIMEOUT seconds applies.

    SSE events:
      {type: "status", key: "...", message: "..."}
      {type: "approval_required", action_id: "...", action: "...", args: {...}, description: "..."}
      {type: "done", answer: "...", iterations: N, tcu: ..., cost_usd: ..., duration_ms: ...}
      {type: "error", message: "..."}
    """
    resolved_plan = (user_plan or settings.default_user_plan).lower()
    _cancel_on_exit = reservation_id is not None

    try:
        # ── Planning status event ─────────────────────────────────────────────
        yield status_event("activity.work.preparing", "Preparando tarefa")

        messages: list[dict] = [
            {"role": "system", "content": WORK_TASK_SYSTEM_PROMPT},
            {"role": "user", "content": task},
        ]

        tool_registry = _build_work_tool_registry()
        work_tools = tool_registry.litellm_specs()

        effective_limit = min(max(1, max_iterations), 20)

        total_input_tokens = 0
        total_output_tokens = 0
        total_cached_tokens = 0
        total_cost_usd = 0.0
        total_tcu = 0.0
        iterations_done = 0
        routing_result = None
        final_answer = ""
        start_time = time.time()

        def _keep_spend() -> bool:
            return _record_partial_usage(
                db, reservation_id,
                model=routing_result.model if routing_result is not None else "",
                input_tokens=total_input_tokens, output_tokens=total_output_tokens,
                cached_tokens=total_cached_tokens, cost_usd=total_cost_usd, tcu=total_tcu,
                started_at=start_time,
            )

        granted_budget_tcu: float | None = None

        try:
            for iteration in range(effective_limit):
                iterations_done = iteration + 1

                # Same guard as code_service.stream_code_task: each call may cost at most what is
                # left of the reservation, so one reservation cannot authorise the whole loop. The
                # grant is read once — check_agent_iteration_budget rewrites the reservation's tcu to
                # the running total, so re-reading it would cap call N+1 at what call N already spent.
                call_budget_usd: float | None = None
                if reservation_id:
                    if granted_budget_tcu is None:
                        granted_budget_tcu = await asyncio.to_thread(
                            get_reservation_budget_tcu, db, reservation_id
                        )
                    call_budget_usd = max(0.0, granted_budget_tcu - total_tcu) / 100.0

                routing_result = await _router.complete(RoutingRequest(
                    messages=messages,
                    task_type=TaskType.AGENT,
                    user_plan=resolved_plan,
                    session_id=workspace_id or "",
                    tools=work_tools,
                    tool_choice="auto",
                    # The workspace IS the capability (base 15, not chat's 0).
                    capability="work",
                    max_cost_usd_override=call_budget_usd,
                ))

                total_input_tokens += routing_result.input_tokens
                total_output_tokens += routing_result.output_tokens
                total_cached_tokens += routing_result.cached_tokens
                total_cost_usd += routing_result.cost_usd
                total_tcu += routing_result.tcu

                if not routing_result.tool_calls:
                    final_answer = routing_result.content or ""
                    break

                messages.append({
                    "role": "assistant",
                    "content": routing_result.content,
                    "tool_calls": routing_result.tool_calls,
                })

                for tc in routing_result.tool_calls:
                    tool_name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    logger.info(
                        "work_task tool_call | workspace=%s tool=%s",
                        workspace_id, tool_name,
                    )

                    tool = tool_registry.get(tool_name)
                    if tool is not None and tool.requires_approval:
                        # ── Approval gate ─────────────────────────────────────
                        action_id = str(_uuid_module.uuid4())
                        gate = _create_gate(action_id, workspace_id or "")

                        description = _describe_confirm_action(tool_name, args)
                        yield _sse({
                            "type": "approval_required",
                            "action_id": action_id,
                            "action": tool_name,
                            "args": args,
                            "description": description,
                        })
                        yield tool_status_event(tool_name)

                        approved = await _wait_for_gate(action_id, gate)

                        if not approved:
                            # The calls that led here were billed; a rejected
                            # approval ends the task but must not refund them.
                            if _keep_spend():
                                _cancel_on_exit = False
                            yield status_event("activity.work.rejected", "Ação rejeitada")
                            yield _sse({"type": "error", "message": "Ação rejeitada ou tempo de aprovação esgotado."})
                            return

                        yield status_event("activity.work.approved", "Ação aprovada")
                        # Executes the REAL tool now that approval is granted
                        # (was a hardcoded canned string before this etapa —
                        # SendNotification/ScheduleAction's own run() today
                        # still just returns that same canned text, since
                        # neither has a real backing action yet, but this
                        # path now calls whatever run() actually does, so a
                        # future Connector-backed implementation is honored
                        # automatically with no change needed here).
                        result = await tool_registry.call(tool_name, args)
                        llm_content = result.output if result.success else f"ERROR: {result.error}"

                    else:
                        yield tool_status_event(tool_name)

                        result = await tool_registry.call(tool_name, args)
                        output = result.output if result.success else result.error
                        truncated = output[:_MAX_OUTPUT_CHARS] if len(output) > _MAX_OUTPUT_CHARS else output

                        logger.info(
                            "work_task tool_result | workspace=%s tool=%s success=%s",
                            workspace_id, tool_name, result.success,
                        )

                        llm_content = truncated if result.success else f"ERROR: {truncated}"

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": llm_content,
                    })

                # About to call the model again: re-check every window against
                # the real running total. A refusal stops the loop and keeps
                # what was spent.
                if reservation_id and user_id:
                    budget_ok = await asyncio.to_thread(
                        check_agent_iteration_budget,
                        db, reservation_id, user_id, resolved_plan, total_tcu, user_timezone,
                    )
                    if not budget_ok:
                        logger.warning(
                            "work_task: stopped, TCU budget exhausted mid-loop | "
                            "workspace=%s iteration=%d tcu=%.4f",
                            workspace_id, iterations_done, total_tcu,
                        )
                        yield status_event("activity.work.preparing", "Limite de uso atingido")
                        final_answer = (
                            "Parei porque continuar excederia o seu limite de uso. "
                            "O trabalho feito até aqui foi salvo."
                        )
                        break

            else:
                yield status_event("activity.work.preparing", "Limite de iterações atingido")
                final_answer = routing_result.content or "" if routing_result else ""

        except BudgetExceededError:
            if _keep_spend():
                _cancel_on_exit = False
            yield _sse(safe_error_payload("request_too_large"))
            return
        except ProviderUnavailableError:
            if _keep_spend():
                _cancel_on_exit = False
            yield _sse(safe_error_payload("temporarily_unavailable"))
            return
        except Exception:
            logger.exception("Unexpected error in work_task loop | workspace=%s", workspace_id)
            if _keep_spend():
                _cancel_on_exit = False
            yield _sse({"type": "error", "message": "Erro interno durante a execução da tarefa."})
            return

        duration_ms = int((time.time() - start_time) * 1000)
        final_model = routing_result.model if routing_result is not None else ""

        if reservation_id:
            finalize_usage(
                db,
                reservation_id,
                message_id=None,
                model=final_model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cached_tokens=total_cached_tokens,
                cost_usd=total_cost_usd,
                tcu=total_tcu,
                duration_ms=duration_ms,
            )
        else:
            from src.services.usage_service import record_usage
            record_usage(
                db=db,
                user_id="",
                session_id=None,
                message_id=None,
                model=final_model,
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cached_tokens=total_cached_tokens,
                cost_usd=total_cost_usd,
                tcu=total_tcu,
                duration_ms=duration_ms,
            )

        _cancel_on_exit = False

        logger.info(
            "work_task completed | workspace=%s model=%s plan=%s iterations=%d "
            "tcu=%.4f cost_usd=%.6f duration_ms=%d",
            workspace_id, final_model, resolved_plan, iterations_done,
            total_tcu, total_cost_usd, duration_ms,
        )

        yield _sse({
            "type": "done",
            "answer": redact_self_identification(final_answer),
            "iterations": iterations_done,
            "duration_ms": duration_ms,
        })
        yield "data: [DONE]\n\n"

    finally:
        if _cancel_on_exit and reservation_id:
            cancel_reservation(db, reservation_id)


def _describe_confirm_action(tool_name: str, args: dict) -> str:
    """Return a plain-language description of a CONFIRM-level action for the approval gate."""
    if tool_name == "send_notification":
        dest = args.get("destination", "")
        msg = args.get("message", "")[:100]
        return f"Enviar mensagem para '{dest}': \"{msg}\""
    if tool_name == "schedule_action":
        action = args.get("action", "")[:100]
        when = args.get("when", "")
        return f"Agendar ação para {when}: {action}"
    return f"Executar ação: {tool_name}"
