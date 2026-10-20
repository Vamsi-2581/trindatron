from __future__ import annotations

import re
from pathlib import Path

from trinity_sandbox import DockerSandbox

from .base import BaseTool, SideEffect, ToolResult

_SANDBOX_IMAGE = "trinity-sandbox:latest"
_SANDBOX_MEMORY = "256m"
_SANDBOX_CPU = "0.5"
_SANDBOX_PIDS = 64

# Deny-list for shell commands.
# NOTE: This is defense-in-depth only. A proper production deployment MUST run
# the agent inside a container or VM. The deny-list cannot enumerate every bypass.
_FORBIDDEN = [
    # ── System destruction ─────────────────────────────────────────────────────
    re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),
    re.compile(r"rmdir\s+/s\s+/q\s+[a-zA-Z]:\\", re.IGNORECASE),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\b.+\bif=.+\bof="),
    re.compile(r":\(\)\s*\{"),                   # bash fork bomb (function named ':')
    re.compile(r">\s*/dev/(?:sda|hda|nvme)", re.IGNORECASE),
    re.compile(r"\b(?:shutdown|reboot|halt|poweroff|init\s+0)\b", re.IGNORECASE),
    re.compile(r"\bformat\s+[a-zA-Z]:", re.IGNORECASE),

    # ── Remote code execution via download ────────────────────────────────────
    re.compile(r"\bcurl\b.+\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"\bwget\b.+\|\s*(?:ba)?sh", re.IGNORECASE),
    re.compile(r"\biwr\b.+\biex\b", re.IGNORECASE),     # PS Invoke-WebRequest | Invoke-Expression

    # ── Alternate/privileged shells ────────────────────────────────────────────
    re.compile(r"\bpowershell\b", re.IGNORECASE),        # PowerShell (bypasses all regex patterns)
    re.compile(r"\bpwsh\b", re.IGNORECASE),              # PowerShell Core

    # ── Environment-variable exfiltration ─────────────────────────────────────
    re.compile(r"\benv\b", re.IGNORECASE),               # `env` dumps all vars on Linux/Mac
    re.compile(r"\bprintenv\b", re.IGNORECASE),
    re.compile(r"\bset\b", re.IGNORECASE),               # `set` dumps all vars on Windows cmd.exe
    re.compile(r"\bexport\b", re.IGNORECASE),            # `export` lists exported vars in bash

    # ── Direct references to secret env vars ──────────────────────────────────
    re.compile(r"\$\{?[A-Za-z_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD)\w*\}?"),
    re.compile(r"%[A-Za-z_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD)\w*%"),

    # ── Sensitive file patterns ────────────────────────────────────────────────
    re.compile(r"\.env\b"),                              # .env files (any directory)
    re.compile(r"[/\\]\.ssh[/\\]", re.IGNORECASE),      # SSH private keys
    re.compile(r"[/\\]\.aws[/\\]", re.IGNORECASE),      # AWS credentials
    re.compile(r"[/\\]\.gnupg[/\\]", re.IGNORECASE),    # GPG keys

    # ── Code-based environment exfiltration ───────────────────────────────────
    re.compile(r"\bos\.environ\b", re.IGNORECASE),       # Python
    re.compile(r"\bprocess\.env\b", re.IGNORECASE),      # Node.js
]


def _is_forbidden(command: str) -> bool:
    return any(p.search(command) for p in _FORBIDDEN)


class ExecuteShell(BaseTool):
    name = "execute_shell"
    description = (
        "Run a shell command inside the agent workspace. "
        "Commands that could destroy data, exfiltrate secrets, or escape the sandbox are blocked."
    )
    side_effect = SideEffect.IRREVERSIBLE_ACTION

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = _SANDBOX_IMAGE,
        memory_limit: str = _SANDBOX_MEMORY,
        cpu_limit: str = _SANDBOX_CPU,
        pids_limit: int = _SANDBOX_PIDS,
        max_timeout_seconds: int = 120,
    ):
        """image/memory_limit/cpu_limit/pids_limit/max_timeout_seconds are
        all optional, defaulting to this module's own pre-existing
        constants — every pre-existing caller that constructs
        ExecuteShell(workspace) with no further arguments keeps its exact
        prior behavior. A caller with its own, stricter policy (e.g. Core's
        settings-driven sandbox config, or a tighter timeout ceiling) can
        override any subset without a second ExecuteShell implementation."""
        self._workspace = workspace
        self._image = image
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit
        self._pids_limit = pids_limit
        self._max_timeout_seconds = max_timeout_seconds
        # Instance-level override of the class-level `parameters` so the
        # schema shown to the model always states the REAL enforced max
        # for THIS instance, never a stale hardcoded number — see run()'s
        # own clamp below, which uses the same self._max_timeout_seconds.
        self.parameters = {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute"},
                "timeout": {
                    "type": "integer",
                    "description": f"Timeout in seconds (default 30, max {max_timeout_seconds})",
                    "default": 30,
                },
            },
            "required": ["command"],
        }

    async def run(self, command: str, timeout: int = 30, **_) -> ToolResult:
        if _is_forbidden(command):
            return ToolResult(success=False, error=f"Command blocked by security policy: {command!r}")

        timeout = min(max(1, timeout), self._max_timeout_seconds)
        sandbox = DockerSandbox(
            image=self._image,
            memory_limit=self._memory_limit,
            cpu_limit=self._cpu_limit,
            pids_limit=self._pids_limit,
        )
        if not await sandbox.is_available():
            return ToolResult(success=False, error=sandbox.unavailable_message)

        ok, output = await sandbox.run(command, self._workspace, timeout)
        if ok:
            return ToolResult(success=True, output=output)
        return ToolResult(success=False, error=output)
