"""Core-native audit log for TRINITY CODE execution events.

Design notes:
- Writes to Python structured logger ("trinity.audit") for log-aggregation compatibility.
- In-memory entries are kept for test assertions; they are not persisted across restarts.
- Future: G-6+ will add a DB-backed AuditRecord model for persistent audit history.
- Never log raw secrets: callers must sanitize args before passing (truncate commands,
  strip env-like patterns). The 200-char truncation in code_service is the primary guard.
- Mobile compatibility: this module has zero HTTP/browser dependencies. AuditEntry
  fields map directly to a JSON structure consumable by any REST client.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

logger = logging.getLogger("trinity.audit")


@dataclass
class AuditEntry:
    timestamp: str
    tool_name: str
    args: dict
    result: str
    permission_level: str
    workspace_id: str
    session_id: str
    execution_id: str   # unique per-execution UUID; set even when no session exists
    success: bool

    @classmethod
    def create(
        cls,
        *,
        tool_name: str,
        args: dict,
        result: str,
        permission_level: str,
        workspace_id: str = "",
        session_id: str = "",
        execution_id: str = "",
        success: bool = False,
    ) -> "AuditEntry":
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            args=args,
            result=result,
            permission_level=permission_level,
            workspace_id=workspace_id,
            session_id=session_id,
            execution_id=execution_id,
            success=success,
        )


class AuditLog:
    """In-memory audit log that writes each entry to the "trinity.audit" logger.

    Thread-safety: not thread-safe for concurrent append+read. Acceptable for
    per-request generators; a future DB-backed implementation will handle concurrency.
    """

    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self._entries.append(entry)
        logger.info("AUDIT %s", json.dumps(asdict(entry)))

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def by_tool(self, tool_name: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.tool_name == tool_name]
