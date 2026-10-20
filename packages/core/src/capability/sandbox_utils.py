"""Shared sandbox utilities for Core services.

Re-exports the official sandbox/shell security primitives from trinity_agent
so that agent_service.py, code_service.py, and any future execution service
consume a single implementation instead of maintaining a duplicate copy.

INVARIANTS (do not relax without explicit security review):
- _sandboxed() uses Path.resolve() + is_relative_to() — never startswith().
- _FORBIDDEN_SHELL is the authoritative blocklist; DockerSandbox remains the
  real execution boundary. Both layers must remain active.
"""
from __future__ import annotations

from trinity_agent.tools.filesystem import _sandboxed
from trinity_agent.tools.shell import _FORBIDDEN as _FORBIDDEN_SHELL
from trinity_agent.security.sanitizer import wrap_external_content as _wrap_external

__all__ = ["_FORBIDDEN_SHELL", "_sandboxed", "_wrap_external"]
