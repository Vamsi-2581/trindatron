from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class AuditEntry:
    timestamp: str
    tool_name: str
    args: dict
    result: str
    permission_level: str
    session_id: str = ""

    @classmethod
    def create(cls, tool_name: str, args: dict, result: str, permission_level: str, session_id: str = "") -> "AuditEntry":
        return cls(
            timestamp=datetime.now(timezone.utc).isoformat(),
            tool_name=tool_name,
            args=args,
            result=result,
            permission_level=permission_level,
            session_id=session_id,
        )


class AuditLog:
    def __init__(self, log_path: Path | None = None):
        self._entries: list[AuditEntry] = []
        self._log_path = log_path

    def record(self, entry: AuditEntry) -> None:
        self._entries.append(entry)
        if self._log_path:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry)) + "\n")

    @property
    def entries(self) -> list[AuditEntry]:
        return list(self._entries)

    def by_tool(self, tool_name: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.tool_name == tool_name]
