"""
TRINITY CODE sandbox execution service.

Executes a single shell command inside an isolated DockerSandbox container.
There is no LLM call, no model routing, and no ReAct loop in this service.

Fail-closed guarantee: if DockerSandbox is unavailable, execution is denied.
There is NO fallback to host execution under any circumstances.

Security layers (in order of enforcement):
  1. PermissionGateway — checks CODE_MANIFEST; execute_code=AUTO passes silently.
  2. _FORBIDDEN_SHELL — regex blocklist (26 patterns) rejects dangerous commands.
  3. DockerSandbox — hardware isolation boundary (--network=none, --read-only,
     --cap-drop=ALL, --no-new-privileges, --user 1000:1000).
All three layers must remain active. DockerSandbox is the primary security boundary.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as _uuid_module
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.orm import Session as DBSession
from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    RoutingRequest,
    TaskType,
)
from trinity_sandbox import DockerSandbox

from src.capability.activity_stream import status_event, tool_status_event
from src.capability.audit import AuditEntry, AuditLog
from src.prompting.identity_guard import redact_self_identification, safe_error_payload
from src.capability.permissions import CODE_MANIFEST, PermissionDenied, PermissionGateway
from src.capability.sandbox_utils import _FORBIDDEN_SHELL, _sandboxed
from src.config import settings
from src.models.code_workspace import CodeWorkspace
from src.services.entitlement_service import (
    cancel_reservation,
    check_agent_iteration_budget,
    finalize_usage,
    get_reservation_budget_tcu,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120

# Sandbox execution produces no LLM tokens or TCU cost — accurate, not fictional.
# The string "sandbox" distinguishes these records from LLM model calls.
_SANDBOX_MODEL = "sandbox"

# Module-level singletons — shared across requests for the process lifetime.
# Tests inject fresh instances via the gateway/audit_log parameters.
_gateway = PermissionGateway(CODE_MANIFEST)
_audit_log = AuditLog()

# System paths that must never be mounted as workspace roots.
# If workspace.root_path resolves to one of these, Docker would expose sensitive
# host files (e.g., /etc/passwd) inside the container at /workspace.
# This is defense-in-depth: access control prevents cross-user access, but this
# blocks a legitimate user from accidentally or deliberately pointing their own
# workspace at a sensitive host directory.
_BLOCKED_SYSTEM_PATHS: frozenset[str] = frozenset({
    "/etc", "/usr", "/bin", "/sbin", "/root",
    "/proc", "/sys", "/dev", "/boot",
    "/var/run", "/var/log", "/lib", "/lib64",
})


def _workspace_root_is_safe(p: Path) -> bool:
    """Return False if p is at or below a known sensitive host system path.

    DockerSandbox mounts workspace_path as /workspace:rw inside the container.
    If workspace_path = /etc, the container has read access to host /etc files.
    This check prevents that exposure.

    Normalizes backslashes to forward slashes so the check works on both Linux
    (production) and Windows (development): on Windows, Path("/etc") has
    str() = "\\etc"; after normalization that becomes "/etc" and matches correctly.
    """
    p_str = str(p).replace("\\", "/")
    return not any(
        p_str == blocked or p_str.startswith(blocked + "/")
        for blocked in _BLOCKED_SYSTEM_PATHS
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


async def stream_code_execute(
    db: DBSession,
    workspace: CodeWorkspace,
    command: str,
    timeout: int = _DEFAULT_TIMEOUT,
    workspace_id: str | None = None,
    user_plan: str | None = None,
    reservation_id: str | None = None,
    gateway: PermissionGateway | None = None,
    audit_log: AuditLog | None = None,
) -> AsyncGenerator[str, None]:
    """
    Execute `command` inside the DockerSandbox and yield SSE-formatted events.

    Event sequence on success:  tool_status → output → done → [DONE]
    Event sequence on failure:  error → (generator exits, finally cancels reservation)

    `reservation_id` must be created by check_and_reserve() in the route handler
    BEFORE StreamingResponse is returned — this function never calls
    check_and_reserve() itself.

    On success:  finalize_usage(tcu=0.0, cost_usd=0.0) — sandbox has no LLM cost.
    On failure:  cancel_reservation() — quota is returned to the user.

    `gateway` and `audit_log` default to module-level singletons; tests inject
    fresh instances to avoid shared state between test cases.
    """
    _gw = gateway or _gateway
    _al = audit_log or _audit_log

    # Unique identifier for this execution, independent of session.
    # workspace/code execute has no session context; execution_id ensures each
    # audit entry is individually traceable without repurposing session_id.
    execution_id = str(_uuid_module.uuid4())

    # Flag is True while the reservation is still pending; set to False only
    # after finalize_usage() succeeds. The finally block uses it to cancel on
    # any exit path that did not reach finalization (error, timeout, GeneratorExit).
    _cancel_on_exit = reservation_id is not None

    # Audit tracking — set through the execution; recorded in the finally block.
    _audit_result = "error"
    _audit_success = False

    try:
        # ── Permission gateway check ──────────────────────────────────────────
        # execute_code is AUTO in CODE_MANIFEST: this passes silently.
        # Any tool not in CODE_MANIFEST defaults to CONFIRM, which raises here
        # (no UI callback exists in an SSE stream).
        try:
            _gw.check("execute_code", {"command": command})
        except PermissionDenied as exc:
            _audit_result = f"permission_denied: {exc}"
            yield _sse({"type": "error", "message": "Execução bloqueada por política de permissão."})
            return

        # ── Validate timeout ─────────────────────────────────────────────────
        if timeout <= 0:
            _audit_result = "invalid_timeout"
            yield _sse({"type": "error", "message": "Timeout inválido: deve ser maior que zero."})
            return

        safe_timeout = min(timeout, _MAX_TIMEOUT)

        # ── Validate workspace configuration ─────────────────────────────────
        if not workspace.root_path:
            _audit_result = "unconfigured_workspace"
            yield _sse({
                "type": "error",
                "message": "Workspace não configurado: defina root_path antes de executar.",
            })
            return

        # Check the raw path BEFORE resolving. On Windows, Path("/etc").resolve()
        # returns C:\etc (misses the blocklist); checking the raw string catches
        # Unix-style blocked paths on all platforms. The post-resolve check below
        # additionally catches symlink-redirected attacks on Linux production.
        if not _workspace_root_is_safe(Path(workspace.root_path)):
            _audit_result = "forbidden_workspace_path"
            yield _sse({
                "type": "error",
                "message": "Workspace path aponta para diretório protegido do sistema.",
            })
            return

        workspace_path = Path(workspace.root_path).resolve()

        if not _workspace_root_is_safe(workspace_path):
            _audit_result = "forbidden_workspace_path"
            yield _sse({
                "type": "error",
                "message": "Workspace path aponta para diretório protegido do sistema.",
            })
            return

        workspace_path.mkdir(parents=True, exist_ok=True)

        # ── Check forbidden commands ──────────────────────────────────────────
        if any(p.search(command) for p in _FORBIDDEN_SHELL):
            _audit_result = "blocked_by_security_policy"
            yield _sse({"type": "error", "message": "Comando bloqueado por política de segurança."})
            return

        # ── Build sandbox ─────────────────────────────────────────────────────
        # workspace.sandbox_image overrides global default when set.
        image = workspace.sandbox_image or settings.agent_sandbox_image
        sandbox = DockerSandbox(
            image=image,
            memory_limit=settings.agent_sandbox_memory_limit,
            cpu_limit=settings.agent_sandbox_cpu_limit,
            pids_limit=settings.agent_sandbox_pids_limit,
        )

        # ── Fail-closed availability check ────────────────────────────────────
        # If Docker daemon is not reachable or the image is missing, deny execution.
        # There is no fallback path. The sandbox MUST be available.
        if not await sandbox.is_available():
            _audit_result = "sandbox_unavailable"
            yield _sse({
                "type": "error",
                "message": "Sandbox indisponível. Verifique se o Docker Desktop está em execução.",
            })
            return

        # ── Execute inside DockerSandbox ──────────────────────────────────────
        yield _sse({"type": "tool_status", "status": "running", "message": "Executando comando"})

        start_time = time.time()
        success, raw_output = await sandbox.run(command, workspace_path, safe_timeout)
        duration_ms = int((time.time() - start_time) * 1000)

        if not success:
            if f"timed out after {safe_timeout}s" in raw_output:
                _audit_result = f"execution_timeout_after_{safe_timeout}s"
                yield _sse({"type": "error", "message": f"Comando expirou após {safe_timeout}s."})
            else:
                _audit_result = "execution_failed"
                yield _sse({"type": "error", "message": "Execução falhou."})
            return

        # ── Stream output ─────────────────────────────────────────────────────
        yield _sse({"type": "output", "stream": "stdout", "data": raw_output})

        # ── Finalize entitlement record ───────────────────────────────────────
        # Sandbox execution has no LLM cost (tcu=0, cost_usd=0). This is the
        # real value — not an estimate or placeholder.
        if reservation_id:
            finalize_usage(
                db,
                reservation_id,
                message_id=None,
                model=_SANDBOX_MODEL,
                input_tokens=0,
                output_tokens=0,
                cached_tokens=0,
                cost_usd=0.0,
                tcu=0.0,
                duration_ms=duration_ms,
            )

        _cancel_on_exit = False
        _audit_result = "success"
        _audit_success = True

        logger.info(
            "code_execute completed | workspace=%s image=%s timeout=%d duration_ms=%d",
            workspace_id, image, safe_timeout, duration_ms,
        )

        yield _sse({
            "type": "done",
            "success": True,
            "exit_code": 0,
            "duration_ms": duration_ms,
        })
        yield "data: [DONE]\n\n"

    finally:
        # Audit is always recorded — success, failure, permission denied, or interrupt.
        # command is truncated to 200 chars: avoids logging sensitive env-like patterns
        # that slipped past _FORBIDDEN_SHELL (belt-and-suspenders).
        _al.record(AuditEntry.create(
            tool_name="execute_code",
            args={"command": command[:200], "timeout": timeout},
            result=_audit_result,
            permission_level=_gw.level("execute_code").value,
            workspace_id=workspace_id or "",
            session_id="",
            execution_id=execution_id,
            success=_audit_success,
        ))
        if _cancel_on_exit and reservation_id:
            cancel_reservation(db, reservation_id)


# ── TRINITY CODE — AI Task Execution ─────────────────────────────────────────

_router = ModelRouter()

_MAX_READ_BYTES = 512_000  # 500 KB per file read — same cap as agent_service
_MAX_OUTPUT_CHARS = 4_000  # truncate large tool outputs before feeding back to LLM

CODE_TASK_SYSTEM_PROMPT = (
    "You are TRINITY CODE, the software-development mode of TRINDATRON. TRINITY CODE is a "
    "technical component name, not the product name — if asked what product or company this "
    "is, answer TRINDATRON. "
    "You have access to tools to read files, write files, list directories, and "
    "execute shell commands. All file paths are relative to the workspace root. "
    "Guidelines:\n"
    "1. Read relevant files before making changes.\n"
    "2. Write complete, working code that follows existing patterns.\n"
    "3. Run tests or validation commands when available.\n"
    "4. When the task is complete, provide a concise summary of what was done.\n"
    "5. Do NOT call any tool once you have finished — respond directly."
)

_CODE_TASK_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the content of a file in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative path inside workspace"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file in the workspace. Creates parent directories as needed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "Full content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories in the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative sub-path (default '.')"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "Run a shell command in the workspace via Docker sandbox. Use for tests, builds, git, linters.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds (max 60)", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
]


async def _run_code_ws_tool(
    name: str,
    args: dict,
    workspace_path: Path,
) -> tuple[bool, str]:
    """Execute a single tool call scoped to workspace_path.

    Security invariant: _sandboxed() is called for every file operation before
    any filesystem access. Path traversal is blocked server-side — the check is
    never delegated to the client or to the LLM-provided path argument.

    For execute_shell: _FORBIDDEN_SHELL blocklist + DockerSandbox are both active.
    If the sandbox is unavailable, the tool returns an error (no host execution fallback).
    """
    if name == "read_file":
        path = args.get("path", "")
        ok, target, err = _sandboxed(workspace_path, path)
        if not ok:
            return False, err
        try:
            size = target.stat().st_size
            if size > _MAX_READ_BYTES:
                return False, f"File too large ({size:,} bytes > {_MAX_READ_BYTES:,} byte limit)"
            return True, target.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return False, f"File not found: {path}"
        except Exception as exc:
            return False, str(exc)

    if name == "write_file":
        path = args.get("path", "")
        content = args.get("content", "")
        ok, target, err = _sandboxed(workspace_path, path)
        if not ok:
            return False, err
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return True, f"Written {len(content)} chars to {path}"
        except Exception as exc:
            return False, str(exc)

    if name == "list_directory":
        path = args.get("path", ".")
        ok, target, err = _sandboxed(workspace_path, path)
        if not ok:
            return False, err
        try:
            entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name))
            lines = [f"[{'FILE' if e.is_file() else 'DIR '}] {e.name}" for e in entries]
            return True, "\n".join(lines) or "(empty)"
        except FileNotFoundError:
            return False, f"Directory not found: {path}"
        except Exception as exc:
            return False, str(exc)

    if name == "execute_shell":
        command = args.get("command", "")
        if any(p.search(command) for p in _FORBIDDEN_SHELL):
            return False, "Command blocked by security policy"
        timeout = min(max(1, args.get("timeout", 30)), 60)
        sandbox = DockerSandbox(
            image=settings.agent_sandbox_image,
            memory_limit=settings.agent_sandbox_memory_limit,
            cpu_limit=settings.agent_sandbox_cpu_limit,
            pids_limit=settings.agent_sandbox_pids_limit,
        )
        if not await sandbox.is_available():
            return False, "Sandbox indisponível: Docker não está acessível no momento."
        return await sandbox.run(command, workspace_path, timeout)

    return False, f"Unknown tool: {name!r}"


def _record_partial_usage(
    db: DBSession,
    reservation_id: str | None,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_tokens: int,
    cost_usd: float,
    tcu: float,
    started_at: float,
) -> bool:
    """Finalize a reservation with what the loop really spent before it stopped.

    Every provider call in the loop was billed. A loop that ends on an error,
    a refused budget or a rejected approval must therefore record that spend,
    not cancel the reservation: cancelling zeroes it, and the next request is
    admitted as if those calls never happened. Returns True when the
    reservation was finalized, so the caller must not cancel it afterwards.
    """
    if not reservation_id or tcu <= 0:
        return False
    finalize_usage(
        db,
        reservation_id,
        message_id=None,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        cost_usd=cost_usd,
        tcu=tcu,
        duration_ms=int((time.time() - started_at) * 1000),
    )
    return True


async def stream_code_task(
    db: DBSession,
    workspace: CodeWorkspace,
    task: str,
    max_iterations: int = 10,
    workspace_id: str | None = None,
    user_plan: str | None = None,
    reservation_id: str | None = None,
    user_id: str | None = None,
    user_timezone: str | None = None,
) -> AsyncGenerator[str, None]:
    """Execute an AI-driven development task inside the CODE workspace via SSE.

    Security guarantees:
    - All file operations go through _sandboxed(); path traversal is blocked
      before any filesystem access.
    - execute_shell uses _FORBIDDEN_SHELL blocklist + DockerSandbox isolation.
    - max_iterations caps the ReAct loop (default 10, request max 20).
    - No LLM response is trusted for path validation — validation is server-side.

    SSE event sequence:
      status (planning) → status (tool events) × N → done | error
    """
    resolved_plan = (user_plan or settings.default_user_plan).lower()
    _cancel_on_exit = reservation_id is not None

    try:
        # ── Workspace validation ──────────────────────────────────────────────
        if not workspace.root_path:
            yield _sse({"type": "error", "message": "Workspace root_path não configurado."})
            return

        if not _workspace_root_is_safe(Path(workspace.root_path)):
            yield _sse({"type": "error", "message": "Workspace aponta para diretório protegido do sistema."})
            return

        workspace_path = Path(workspace.root_path).resolve()

        if not _workspace_root_is_safe(workspace_path):
            yield _sse({"type": "error", "message": "Workspace aponta para diretório protegido do sistema."})
            return

        workspace_path.mkdir(parents=True, exist_ok=True)

        # ── Planning status event ─────────────────────────────────────────────
        yield status_event("activity.code.planning", "Planejando implementação")

        # ── Build initial messages ────────────────────────────────────────────
        system_prompt = (
            f"{CODE_TASK_SYSTEM_PROMPT}\n\n"
            f"## Workspace\nRoot: {workspace_path}\n"
        )
        if workspace.language_hint:
            system_prompt += f"Language: {workspace.language_hint}\n"
        if workspace.test_command:
            system_prompt += f"Test command: {workspace.test_command}\n"

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]

        # ── ReAct loop ────────────────────────────────────────────────────────
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

                # The loop spends as it goes, so every call is capped at what is LEFT of the
                # reservation — agent_service.py's pattern. Without a cap, one reservation authorised
                # up to 20 calls: a task started on a sliver of balance could spend far past the
                # plan's weekly ceiling before finalize_usage() ever recorded it. The grant is read
                # once, because check_agent_iteration_budget below rewrites the reservation's tcu to
                # the running total; re-reading it capped each call at what the previous one spent.
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
                    tools=_CODE_TASK_TOOLS,
                    tool_choice="auto",
                    # The workspace IS the capability. Without it the router
                    # scored complexity as if this were chat (base 0, not 20)
                    # and discovered candidates for no capability at all.
                    capability="code",
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

                    yield tool_status_event(tool_name)

                    logger.info(
                        "code_task tool_call | workspace=%s tool=%s",
                        workspace_id, tool_name,
                    )

                    ok, output = await _run_code_ws_tool(tool_name, args, workspace_path)
                    truncated = output[:_MAX_OUTPUT_CHARS] if len(output) > _MAX_OUTPUT_CHARS else output

                    logger.info(
                        "code_task tool_result | workspace=%s tool=%s success=%s",
                        workspace_id, tool_name, ok,
                    )

                    llm_content = truncated if ok else f"ERROR: {truncated}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": llm_content,
                    })

                # About to call the model again: re-check every window against
                # the real running total, under the same lock check_and_reserve
                # uses. A refusal stops the loop and keeps what was spent.
                if reservation_id and user_id:
                    budget_ok = await asyncio.to_thread(
                        check_agent_iteration_budget,
                        db, reservation_id, user_id, resolved_plan, total_tcu, user_timezone,
                    )
                    if not budget_ok:
                        logger.warning(
                            "code_task: stopped, TCU budget exhausted mid-loop | "
                            "workspace=%s iteration=%d tcu=%.4f",
                            workspace_id, iterations_done, total_tcu,
                        )
                        yield status_event("activity.code.limit_reached", "Limite de uso atingido")
                        final_answer = (
                            "Parei porque continuar excederia o seu limite de uso. "
                            "O trabalho feito até aqui foi salvo."
                        )
                        break

            else:
                # Exhausted iteration limit without a final answer.
                yield status_event("activity.code.limit_reached", "Limite de iterações atingido")
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
            logger.exception("Unexpected error in code_task loop | workspace=%s", workspace_id)
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
            "code_task completed | workspace=%s model=%s plan=%s iterations=%d "
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
