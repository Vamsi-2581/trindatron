"""Permission gateway for TRINITY CODE execution.

Architecture note — why execute_code = AUTO:
    In the agent context, execute_shell=CONFIRM exists because an LLM may autonomously
    decide to run arbitrary shell commands without explicit human intent. The permission
    gateway is the human gate between LLM decision and host execution.

    In the CODE context, the situation is fundamentally different:
        1. The user explicitly provides every command (no LLM autonomy in execution choice).
        2. DockerSandbox enforces hardware-level isolation: --network=none, --read-only,
           --cap-drop=ALL, --no-new-privileges, --user 1000:1000.
        3. _FORBIDDEN_SHELL regex (26 patterns) blocks dangerous commands BEFORE the
           sandbox even starts.
        4. There is NO interactive confirmation UI in an SSE stream — any CONFIRM level
           would deny-by-default, making the endpoint completely unusable.

    The DockerSandbox is the security boundary for CODE. The gateway records the decision.

Mobile compatibility: this module has no HTTP/browser dependencies. PermissionLevel
values are plain strings serializable to JSON, consumable by any REST client.
"""
from __future__ import annotations

# PermissionLevel and PermissionDenied are byte-identical to trinity_agent's
# definitions (same enum members/values, same bare exception) — re-exported
# here instead of duplicated, same pattern as sandbox_utils.py. CODE_MANIFEST
# and PermissionGateway below remain Core-specific: this gateway has no
# confirm_callback and never merges over trinity_agent's DEFAULT_MANIFEST.
from trinity_agent.permissions.manifest import PermissionLevel
from trinity_agent.permissions.gateway import PermissionDenied


# CODE workspace permission manifest.
#
# execute_code = AUTO:
#   User intent is explicit (they typed the command). DockerSandbox + _FORBIDDEN_SHELL
#   provide the security boundary. No interactive confirmation UI exists in SSE context.
#
# Extend this manifest when new CODE tools are added (e.g., file_read, git_commit).
# Default for unknown tools is CONFIRM, which raises in CODE context (no callback).
CODE_MANIFEST: dict[str, PermissionLevel] = {
    "execute_code": PermissionLevel.AUTO,
}


class PermissionGateway:
    """Evaluate and enforce tool permissions for CODE workspace execution.

    Unlike the agent's PermissionGateway, this implementation has no confirm_callback.
    In the CODE SSE context there is no interactive UI, so CONFIRM is equivalent to
    FORBIDDEN — both raise PermissionDenied. This is intentional: an unknown tool that
    somehow reaches this gateway must be explicitly added to CODE_MANIFEST before use.
    """

    def __init__(self, manifest: dict[str, PermissionLevel]) -> None:
        self._manifest = dict(manifest)

    def level(self, tool_name: str) -> PermissionLevel:
        return self._manifest.get(tool_name, PermissionLevel.CONFIRM)

    def check(self, tool_name: str, args: dict) -> None:  # noqa: ARG002
        """Raise PermissionDenied if the tool cannot proceed.

        AUTO → passes silently (audit happens in the caller).
        NOTIFY → passes silently (audit happens in the caller).
        CONFIRM → raises (no interactive callback in CODE SSE context).
        FORBIDDEN → raises.
        """
        lvl = self.level(tool_name)
        if lvl == PermissionLevel.FORBIDDEN:
            raise PermissionDenied(f"'{tool_name}' is forbidden by policy")
        if lvl == PermissionLevel.CONFIRM:
            raise PermissionDenied(
                f"'{tool_name}' requires explicit confirmation; "
                "add it to CODE_MANIFEST with AUTO or NOTIFY to enable"
            )
        # AUTO and NOTIFY: pass through silently.
