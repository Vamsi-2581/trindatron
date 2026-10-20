"""Unit tests for src.capability.permissions — PermissionGateway and CODE_MANIFEST."""
from __future__ import annotations

import pytest

from src.capability.permissions import (
    CODE_MANIFEST,
    PermissionDenied,
    PermissionGateway,
    PermissionLevel,
)


# ── CODE_MANIFEST ─────────────────────────────────────────────────────────────

def test_execute_code_is_auto_in_code_manifest():
    assert CODE_MANIFEST["execute_code"] == PermissionLevel.AUTO


def test_code_manifest_has_no_forbidden_execute_code():
    assert CODE_MANIFEST.get("execute_code") != PermissionLevel.FORBIDDEN


def test_code_manifest_is_dict_of_permission_levels():
    for k, v in CODE_MANIFEST.items():
        assert isinstance(k, str)
        assert isinstance(v, PermissionLevel)


# ── PermissionLevel enum ──────────────────────────────────────────────────────

def test_permission_level_values():
    assert PermissionLevel.AUTO == "auto"
    assert PermissionLevel.NOTIFY == "notify"
    assert PermissionLevel.CONFIRM == "confirm"
    assert PermissionLevel.FORBIDDEN == "forbidden"


def test_permission_level_is_str_subclass():
    assert isinstance(PermissionLevel.AUTO, str)


# ── PermissionGateway.level ───────────────────────────────────────────────────

def test_gateway_level_returns_correct_level():
    gw = PermissionGateway(CODE_MANIFEST)
    assert gw.level("execute_code") == PermissionLevel.AUTO


def test_gateway_level_unknown_tool_defaults_to_confirm():
    gw = PermissionGateway(CODE_MANIFEST)
    assert gw.level("unknown_tool") == PermissionLevel.CONFIRM


def test_gateway_level_respects_custom_manifest():
    custom = {"custom_tool": PermissionLevel.NOTIFY}
    gw = PermissionGateway(custom)
    assert gw.level("custom_tool") == PermissionLevel.NOTIFY


# ── PermissionGateway.check — AUTO ───────────────────────────────────────────

def test_gateway_check_auto_passes_silently():
    gw = PermissionGateway(CODE_MANIFEST)
    gw.check("execute_code", {"command": "ls"})  # must not raise


def test_gateway_check_auto_passes_with_any_args():
    gw = PermissionGateway(CODE_MANIFEST)
    gw.check("execute_code", {"command": "python3 --version", "timeout": 30})


def test_gateway_check_notify_passes_silently():
    gw = PermissionGateway({"notify_tool": PermissionLevel.NOTIFY})
    gw.check("notify_tool", {})  # must not raise


# ── PermissionGateway.check — FORBIDDEN ──────────────────────────────────────

def test_gateway_check_forbidden_raises():
    gw = PermissionGateway({"bad_tool": PermissionLevel.FORBIDDEN})
    with pytest.raises(PermissionDenied, match="forbidden by policy"):
        gw.check("bad_tool", {})


def test_gateway_check_forbidden_raises_with_tool_name_in_message():
    gw = PermissionGateway({"forbidden_cmd": PermissionLevel.FORBIDDEN})
    with pytest.raises(PermissionDenied, match="forbidden_cmd"):
        gw.check("forbidden_cmd", {"command": "something"})


# ── PermissionGateway.check — CONFIRM (deny-by-default in CODE context) ──────

def test_gateway_check_confirm_raises_in_code_context():
    gw = PermissionGateway({"confirm_tool": PermissionLevel.CONFIRM})
    with pytest.raises(PermissionDenied, match="requires explicit confirmation"):
        gw.check("confirm_tool", {})


def test_gateway_check_unknown_tool_raises():
    gw = PermissionGateway(CODE_MANIFEST)
    with pytest.raises(PermissionDenied):
        gw.check("unknown_unlisted_tool", {})


# ── PermissionGateway — manifest isolation ────────────────────────────────────

def test_gateway_does_not_mutate_original_manifest():
    original = dict(CODE_MANIFEST)
    gw = PermissionGateway(CODE_MANIFEST)
    # Modifying internal dict should not affect CODE_MANIFEST
    gw._manifest["execute_code"] = PermissionLevel.FORBIDDEN
    assert CODE_MANIFEST["execute_code"] == PermissionLevel.AUTO


def test_permission_denied_is_exception():
    assert issubclass(PermissionDenied, Exception)
