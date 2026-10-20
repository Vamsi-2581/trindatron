"""Unit tests for src.capability.audit — AuditEntry and AuditLog."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict

import pytest

from src.capability.audit import AuditEntry, AuditLog


# ── AuditEntry ────────────────────────────────────────────────────────────────

def test_audit_entry_create_sets_timestamp():
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "ls"},
        result="success",
        permission_level="auto",
    )
    assert entry.timestamp  # non-empty ISO 8601
    assert "T" in entry.timestamp  # UTC datetime format


def test_audit_entry_create_defaults():
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={},
        result="success",
        permission_level="auto",
    )
    assert entry.workspace_id == ""
    assert entry.session_id == ""
    assert entry.execution_id == ""
    assert entry.success is False


def test_audit_entry_create_all_fields():
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "echo hi", "timeout": 30},
        result="success",
        permission_level="auto",
        workspace_id="ws-123",
        session_id="sess-456",
        execution_id="exec-789",
        success=True,
    )
    assert entry.tool_name == "execute_code"
    assert entry.args == {"command": "echo hi", "timeout": 30}
    assert entry.result == "success"
    assert entry.permission_level == "auto"
    assert entry.workspace_id == "ws-123"
    assert entry.session_id == "sess-456"
    assert entry.execution_id == "exec-789"
    assert entry.success is True


def test_audit_entry_execution_id_in_json_output():
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={},
        result="success",
        permission_level="auto",
        execution_id="eid-abc-123",
    )
    from dataclasses import asdict
    d = asdict(entry)
    assert "execution_id" in d
    assert d["execution_id"] == "eid-abc-123"


def test_audit_entry_serializable_to_json():
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "ls"},
        result="success",
        permission_level="auto",
        success=True,
    )
    serialized = json.dumps(asdict(entry))
    parsed = json.loads(serialized)
    assert parsed["tool_name"] == "execute_code"
    assert parsed["success"] is True


# ── AuditLog ──────────────────────────────────────────────────────────────────

def test_audit_log_starts_empty():
    log = AuditLog()
    assert log.entries == []


def test_audit_log_record_stores_entry():
    log = AuditLog()
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "ls"},
        result="success",
        permission_level="auto",
    )
    log.record(entry)
    assert len(log.entries) == 1
    assert log.entries[0].tool_name == "execute_code"


def test_audit_log_entries_returns_copy():
    log = AuditLog()
    entry = AuditEntry.create(
        tool_name="execute_code", args={}, result="success", permission_level="auto",
    )
    log.record(entry)
    copy = log.entries
    copy.clear()
    assert len(log.entries) == 1


def test_audit_log_multiple_entries():
    log = AuditLog()
    for i in range(5):
        log.record(AuditEntry.create(
            tool_name="execute_code",
            args={"command": f"cmd-{i}"},
            result="success",
            permission_level="auto",
        ))
    assert len(log.entries) == 5


def test_audit_log_by_tool_filters():
    log = AuditLog()
    log.record(AuditEntry.create(tool_name="execute_code", args={}, result="success", permission_level="auto"))
    log.record(AuditEntry.create(tool_name="read_file", args={}, result="success", permission_level="auto"))
    log.record(AuditEntry.create(tool_name="execute_code", args={}, result="error", permission_level="auto"))

    code_entries = log.by_tool("execute_code")
    assert len(code_entries) == 2
    assert all(e.tool_name == "execute_code" for e in code_entries)


def test_audit_log_by_tool_unknown_returns_empty():
    log = AuditLog()
    assert log.by_tool("nonexistent_tool") == []


def test_audit_log_record_writes_to_logger(caplog):
    log = AuditLog()
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "echo test"},
        result="success",
        permission_level="auto",
        workspace_id="ws-1",
        success=True,
    )
    with caplog.at_level(logging.INFO, logger="trinity.audit"):
        log.record(entry)

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.name == "trinity.audit"
    assert "execute_code" in record.message


def test_audit_log_logger_message_contains_json(caplog):
    log = AuditLog()
    entry = AuditEntry.create(
        tool_name="execute_code",
        args={"command": "ls", "timeout": 30},
        result="success",
        permission_level="auto",
    )
    with caplog.at_level(logging.INFO, logger="trinity.audit"):
        log.record(entry)

    msg = caplog.records[0].message
    # Should be parseable JSON after "AUDIT " prefix
    json_part = msg[len("AUDIT "):]
    parsed = json.loads(json_part)
    assert parsed["tool_name"] == "execute_code"
    assert parsed["result"] == "success"
