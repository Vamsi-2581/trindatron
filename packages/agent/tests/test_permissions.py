import pytest
from trinity_agent.permissions.manifest import PermissionLevel, DEFAULT_MANIFEST
from trinity_agent.permissions.gateway import PermissionGateway, PermissionDenied


def test_default_manifest_has_expected_tools():
    assert "read_file" in DEFAULT_MANIFEST
    assert "execute_shell" in DEFAULT_MANIFEST
    assert "web_search" in DEFAULT_MANIFEST


def test_read_file_is_auto_by_default():
    assert DEFAULT_MANIFEST["read_file"] == PermissionLevel.AUTO


def test_write_file_requires_confirm_by_default():
    assert DEFAULT_MANIFEST["write_file"] == PermissionLevel.CONFIRM


def test_execute_shell_requires_confirm_by_default():
    assert DEFAULT_MANIFEST["execute_shell"] == PermissionLevel.CONFIRM


def test_auto_tool_passes_silently():
    gw = PermissionGateway(manifest={"my_tool": PermissionLevel.AUTO})
    gw.check("my_tool", {})  # should not raise


def test_notify_calls_callback():
    notified = []
    gw = PermissionGateway(
        manifest={"my_tool": PermissionLevel.NOTIFY},
        notify_callback=lambda name, args: notified.append(name),
    )
    gw.check("my_tool", {"x": 1})
    assert notified == ["my_tool"]


def test_confirm_denied_by_default():
    gw = PermissionGateway(manifest={"my_tool": PermissionLevel.CONFIRM})
    with pytest.raises(PermissionDenied, match="denied"):
        gw.check("my_tool", {})


def test_confirm_approved_when_callback_returns_true():
    gw = PermissionGateway(
        manifest={"my_tool": PermissionLevel.CONFIRM},
        confirm_callback=lambda name, args: True,
    )
    gw.check("my_tool", {})  # should not raise


def test_forbidden_always_raises():
    gw = PermissionGateway(manifest={"my_tool": PermissionLevel.FORBIDDEN})
    with pytest.raises(PermissionDenied, match="forbidden"):
        gw.check("my_tool", {})


def test_unknown_tool_defaults_to_confirm():
    gw = PermissionGateway()
    assert gw.level("totally_unknown_tool") == PermissionLevel.CONFIRM


def test_custom_manifest_overrides_default():
    gw = PermissionGateway(manifest={"write_file": PermissionLevel.AUTO})
    assert gw.level("write_file") == PermissionLevel.AUTO


def test_permission_level_values():
    assert PermissionLevel.AUTO == "auto"
    assert PermissionLevel.NOTIFY == "notify"
    assert PermissionLevel.CONFIRM == "confirm"
    assert PermissionLevel.FORBIDDEN == "forbidden"
