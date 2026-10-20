"""Shared async approval-gate infrastructure.

Security etapa (post Tool/Connector Foundation, commit be6e1b0): TRINITY
WORK already had a real, working async approval gate
(per-action-UUID -> asyncio.Event -> SSE pause/resume -> a resolve HTTP
endpoint) living inline in work_service.py. TRINITY AGENT needed the
SAME mechanism (to actually enforce trinity_agent.permissions.manifest's
CONFIRM level for write_file/execute_shell, previously declared but
never enforced anywhere reachable from Core — see the Tool/Connector
Foundation report's critical finding) — rather than hand-rolling a
SECOND, near-identical gate, this module generalizes the one WORK
already proved out. work_service.py's own _PendingGate/_wait_for_gate/
resolve_gate now delegate here as thin, byte-for-byte-compatible
wrappers (see that module for how its external API — resolve_gate's
`workspace_id` keyword name included — stays unchanged).

`owner_id` generalizes WORK's `workspace_id` / AGENT's `session_id`: the
one caller-supplied string that must match between gate creation and
resolution, preventing a user from resolving a gate they don't own by
guessing or obtaining the action_id (the resolving HTTP route ALSO
checks DB ownership first, independently — this is defense in depth,
not the only check).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("trinity.approval_gate")

GATE_TIMEOUT = 300  # 5 minutes to approve/reject before the gate times out


@dataclass
class PendingGate:
    owner_id: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    approved: bool | None = None  # Set when event fires


# action_id -> pending gate — module-level dict; tests may inject pre-resolved gates
_pending_gates: dict[str, PendingGate] = {}


def create_gate(action_id: str, owner_id: str) -> PendingGate:
    gate = PendingGate(owner_id=owner_id)
    _pending_gates[action_id] = gate
    return gate


async def wait_for_gate(action_id: str, gate: PendingGate, *, timeout: float = GATE_TIMEOUT) -> bool:
    """Wait for approval or rejection. Returns True if approved, False
    otherwise (rejected or timed out). Extracted as a standalone function
    to allow tests to mock the wait behaviour."""
    try:
        await asyncio.wait_for(gate.event.wait(), timeout=timeout)
        return gate.approved is True
    except asyncio.TimeoutError:
        return False
    finally:
        _pending_gates.pop(action_id, None)


def resolve_gate(action_id: str, *, approved: bool, owner_id: str) -> bool:
    """Resolve a pending approval gate.

    owner_id must match the owner that created the gate — prevents a
    user from resolving another user's gate by guessing or obtaining the
    action_id. Returns True if the gate existed and belonged to this
    owner."""
    gate = _pending_gates.get(action_id)
    if gate is None:
        return False
    if gate.owner_id != owner_id:
        logger.warning(
            "resolve_gate owner mismatch | action=%s expected=%s got=%s",
            action_id, gate.owner_id, owner_id,
        )
        return False
    gate.approved = approved
    gate.event.set()
    return True
