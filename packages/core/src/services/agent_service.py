from __future__ import annotations

import asyncio
import json
import logging
import re as _re
import sys
import time
import uuid as _uuid_module
from pathlib import Path
from typing import AsyncGenerator

from sqlalchemy.orm import Session as DBSession
from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    QualityNotEntitledError,
    RoutingRequest,
    TaskType,
)
from trinity_agent.browser.governor import BrowserResourceGovernor
from trinity_agent.browser.session import BrowserSession
from trinity_sandbox import DesktopSandbox
from trinity_agent.tools.desktop import (
    DesktopScreenshot, DesktopClick, DesktopDoubleClick, DesktopRightClick,
    DesktopType, DesktopKey, DesktopScroll, DesktopDrag, DesktopMove,
    DesktopOpen, DesktopGetWindows,
)
from trinity_agent.permissions.gateway import PermissionGateway
from trinity_agent.permissions.manifest import DEFAULT_MANIFEST, PermissionLevel
from trinity_agent.tools.base import ToolResult
from trinity_agent.tools.browser import (
    BrowserClick,
    BrowserFill,
    BrowserGetText,
    BrowserNavigate,
    BrowserScreenshot,
)
from trinity_agent.tools.filesystem import ListDirectory, ReadFile, WriteFile
from trinity_agent.tools.registry import ToolRegistry
from trinity_agent.tools.search import WebSearch
from trinity_agent.tools.shell import ExecuteShell

from src.capability import Capability, classify, capability_status_event, tool_status_event, get_task_type, UNIMPLEMENTED_CAPABILITIES
from src.capability.activity_stream import tool_activity
from src.capability.multimodal import COMING_SOON_MESSAGES
from src.capability.sandbox_utils import _sandboxed, _wrap_external
from src.config import settings
from src.entitlements import get_plan_entitlement
from src.models.message import Message
from src.models.session import Session
from src.observability.metrics import core_metrics
from src.services.agent_execution_service import (
    mark_completed as mark_execution_completed,
    mark_failed as mark_execution_failed,
    mark_interrupted as mark_execution_interrupted,
    mark_running as mark_execution_running,
)
from src.services.agent_scheduler import AgentExecutionToken
from src.services.approval_gate import create_gate, wait_for_gate
from src.services.continuity_entitlement_service import true_up_continuity
from src.services.entitlement_service import (
    cancel_reservation,
    check_agent_iteration_budget,
    finalize_usage,
    get_reservation_budget_tcu,
)
from src.services.idempotency_service import mark_completed as mark_idempotency_completed
from src.services.idempotency_service import mark_failed as mark_idempotency_failed
from src.services.request_control import RequestControlToken
from src.services.runtime_settings import get_disabled_provider_keys
from src.prompting import assemble, from_tool_schemas
from src.prompting.context_builders import build_relevant_memory, build_task_context
from src.prompting.history import conversation_context
from src.prompting.tool_policy import WEB_RESEARCH_TOOLS
from src.prompting.identity_guard import (
    redact_self_identification,
    safe_error_payload,
)
from src.services.product_answers import product_answer
from src.services.skill_service import ALLOWED_TOOL_IDS, load_skills_for_context

logger = logging.getLogger(__name__)

_router = ModelRouter()

# Security etapa (post Tool/Connector Foundation, commit be6e1b0's own
# critical finding): trinity_agent.permissions.manifest.DEFAULT_MANIFEST
# already declared write_file/execute_shell as PermissionLevel.CONFIRM,
# but nothing reachable from Core ever enforced it — TrinityAgent (the
# orchestrator that DOES enforce PermissionGateway) is a non-streaming,
# synchronous-confirm design never wired into any Core route, and this
# module's own ReAct loop called tools directly with no permission check
# at all. This singleton reuses DEFAULT_MANIFEST as-is (no second,
# Core-specific manifest invented) purely for its `.level()` lookup —
# see stream_agent()'s tool-call loop for where CONFIRM now actually
# pauses the SSE stream via the shared approval_gate module (same
# mechanism TRINITY WORK already used, not a second implementation).
_permission_gateway = PermissionGateway()

# Which tools the model must be TOLD are gated. Derived from the same manifest
# the gateway enforces with, never a second hand-maintained list: a tool whose
# level changes there changes here in the same commit, and the prompt cannot
# claim a gated tool runs freely. This is description, not enforcement — the
# gateway remains the thing that actually pauses.
APPROVAL_REQUIRED_TOOL_NAMES: frozenset[str] = frozenset(
    name for name, level in DEFAULT_MANIFEST.items()
    if level is PermissionLevel.CONFIRM
)

# Root-cause fix (2026-09-02) — repeated-action circuit breaker for the
# ReAct loop below. See settings.agent_repeated_action_failure_limit's own
# docstring in config.py for the full rationale: the loop previously had
# no way to notice the model repeating the exact same failing tool call,
# only the coarse whole-task agent_max_iterations bound. Counted per
# stream_agent() EXECUTION (a plain local dict inside the loop, never
# shared across requests/sessions) — this is intentionally NOT persisted
# or cross-request state; each new /agent call starts a fresh ReAct loop
# with no memory of a previous execution's repeated failures, exactly as
# today for every other in-loop variable (messages, total_tcu, etc.).
_agent_early_stops_total = core_metrics.counter(
    "agent_early_stops_total",
    "ReAct loop executions that stopped before agent_max_iterations was reached, labeled by reason.",
)

# One governor for the whole process — every stream_agent() execution's
# BrowserSession acquires a slot from this same instance before launching
# Chromium, bounding total concurrent browser processes regardless of how
# many sessions/requests are in flight. Sized from settings, same pattern
# as _router above (constructed once at import time, not per-request).
_browser_governor = BrowserResourceGovernor(
    capacity=settings.agent_browser_max_concurrent_sessions,
)

_MAX_READ_BYTES = 512_000  # 500 KB per file read
_MAX_OUTPUT_CHARS = 4_000  # truncate large tool outputs before feeding to LLM

_AGENT_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the text content of a file in the session workspace.",
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
            "description": "Write text content to a file in the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path inside workspace"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "List files and directories in the session workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Relative sub-path (default '.')"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "execute_shell",
            "description": "Run a shell command in the session workspace. Destructive commands and secret exfiltration are blocked.",
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
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using DuckDuckGo and return top results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Number of results (default 5)", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_navigate",
            "description": "Navigate to a URL in a headless browser and return the page title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to navigate to"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_get_text",
            "description": "Extract all visible text from the current browser page.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_screenshot",
            "description": "Take a screenshot of the current browser page and save it to the workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Output filename (default: screenshot.png)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_click",
            "description": (
                "Click an element on the current browser page. Use a CSS selector "
                "or a Playwright text selector (e.g. 'button#enviar', 'text=Aceitar'). "
                "Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS or text selector of the element"},
                },
                "required": ["selector"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "browser_fill",
            "description": (
                "Type a value into an input or textarea on the current browser page. "
                "Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the field"},
                    "value": {"type": "string", "description": "Text to enter"},
                },
                "required": ["selector", "value"],
            },
        },
    },
    # ── Computer use ──────────────────────────────────────────────────
    #
    # These drive a virtual desktop the agent owns: an Xvfb display inside a
    # locked-down container (--network=none, --read-only, --cap-drop=ALL,
    # non-root, no X11 socket from the host). It is NOT the user's own
    # machine and there is no code path from here to one — the isolation is
    # spelled out in packages/sandbox/src/trinity_sandbox/desktop_sandbox.py.
    #
    # Every one of these is CONFIRM in the permission manifest, so each
    # pauses the stream for the user's approval before it runs. They are
    # only offered to the model when the sandbox is genuinely available and
    # the plan includes computer use — see _desktop_available().
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": (
                "Capture the current screen of the virtual desktop as an image in the "
                "workspace. Use this to see what is on screen before acting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Output file name, e.g. screen.png"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_get_windows",
            "description": (
                "List the windows currently open on the virtual desktop."
            ),
            "parameters": {
                "type": "object",
                "properties": {

                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_move",
            "description": (
                "Move the mouse pointer to a coordinate on the virtual desktop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Horizontal pixel position"},
                    "y": {"type": "integer", "description": "Vertical pixel position"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_click",
            "description": (
                "Click at a coordinate on the virtual desktop. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Horizontal pixel position"},
                    "y": {"type": "integer", "description": "Vertical pixel position"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_double_click",
            "description": (
                "Double-click at a coordinate on the virtual desktop. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Horizontal pixel position"},
                    "y": {"type": "integer", "description": "Vertical pixel position"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_right_click",
            "description": (
                "Right-click at a coordinate on the virtual desktop. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Horizontal pixel position"},
                    "y": {"type": "integer", "description": "Vertical pixel position"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_type",
            "description": (
                "Type text on the virtual desktop, into whatever currently has focus. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to type"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_key",
            "description": (
                "Press a key or key combination on the virtual desktop, for example "
                "ctrl plus s. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {"type": "array", "items": {"type": "string"}, "description": "Keys pressed together, e.g. ctrl and c"},
                },
                "required": ["keys"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_scroll",
            "description": (
                "Scroll the virtual desktop. Positive scrolls up, negative scrolls down. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "clicks": {"type": "integer", "description": "Scroll amount; negative scrolls down"},
                },
                "required": ["clicks"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_drag",
            "description": (
                "Drag from one coordinate to another on the virtual desktop. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_x": {"type": "integer"},
                    "start_y": {"type": "integer"},
                    "end_x": {"type": "integer"},
                    "end_y": {"type": "integer"},
                },
                "required": ["start_x", "start_y", "end_x", "end_y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "desktop_open",
            "description": (
                "Open a file from the workspace in the virtual desktop's default application. Requires the user's approval before it runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path"},
                },
                "required": ["path"],
            },
        },
    },
]

# Every tool that drives the virtual desktop. Named once so the spec list,
# the dispatch table, the availability filter and the Continuity fail-closed
# filter cannot drift apart.
DESKTOP_TOOL_NAMES = frozenset({
    "desktop_screenshot", "desktop_get_windows", "desktop_move", "desktop_click",
    "desktop_double_click", "desktop_right_click", "desktop_type", "desktop_key",
    "desktop_scroll", "desktop_drag", "desktop_open",
})

_EXTERNAL_TOOLS = {"web_search", "read_file", "browser_get_text", "browser_navigate"}

# Every tool that drives the headless browser. Named once so the dispatch
# table, the Continuity fail-closed filter and the defence-in-depth check in
# the tool loop can never drift apart — they used to repeat the same literal
# tuple in three places.
_BROWSER_TOOL_NAMES = frozenset({
    "browser_navigate", "browser_get_text", "browser_screenshot",
    "browser_click", "browser_fill",
})


async def _desktop_available(plan: str) -> bool:
    """Whether computer use can be offered on this request.

    Two independent gates, both of which must pass:

      1. The plan includes it. Computer use runs a container per session,
         which is the most expensive thing an agent can ask for, so it rides
         on supports_trinity_work — the existing flag for approval-gated
         autonomous execution (PRO and above). No new entitlement field was
         invented for it.

      2. The sandbox is genuinely reachable: Docker responds and the desktop
         image exists. This is checked rather than assumed because it is
         environment-dependent — a developer machine with Docker Desktop has
         it, and a container platform that does not expose a Docker daemon
         does not.

    When either gate is closed the tools are removed from the manifest the
    model sees, so it never proposes an action that cannot run. That is the
    difference between a capability that is absent and one that is broken.
    """
    # No try/except around the entitlement lookup: get_plan_entitlement()
    # already falls back to the START plan for an unknown name, so there is
    # nothing here that legitimately raises. Wrapping it would only hide a
    # real error as "computer use is off", which is exactly what happened
    # while this was being written — a missing import read as a closed gate
    # for every plan, silently.
    if not get_plan_entitlement(plan).supports_trinity_work:
        return False

    # This one IS defensive on purpose: it shells out to Docker, which can
    # be missing, unreachable, or slow, and none of those are errors — they
    # just mean the capability is not available here.
    try:
        return await DesktopSandbox(image=settings.agent_desktop_sandbox_image).image_available()
    except Exception:
        logger.debug("agent: desktop sandbox probe failed", exc_info=True)
        return False


def _workspace_for(session_id: str) -> Path:
    ws = (Path(settings.agent_workspace_root) / session_id).resolve()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


async def _run_tool(
    name: str,
    args: dict,
    workspace: Path,
    browser_session: BrowserSession | None = None,
    desktop_sandbox: DesktopSandbox | None = None,
) -> tuple[bool, str]:
    if name in ("read_file", "write_file", "list_directory", "web_search", "execute_shell"):
        registry = ToolRegistry()
        registry.register(ReadFile(workspace))
        registry.register(WriteFile(workspace))
        registry.register(ListDirectory(workspace))
        registry.register(WebSearch())
        # Tool/Connector Foundation etapa: previously an inline DockerSandbox
        # call here, separate from trinity_agent.tools.shell.ExecuteShell
        # (which existed but was never wired up) — its own _is_forbidden()
        # check already covers what the removed inline
        # `_FORBIDDEN_SHELL` check did (same list, re-exported — see
        # sandbox_utils.py), and its own is_available()/unavailable_message
        # handling already covers what the removed inline DockerSandbox
        # check did. max_timeout_seconds=60 preserves this call site's
        # own PRE-EXISTING stricter cap (ExecuteShell's own standalone
        # default is 120) — image/memory/cpu/pids stay settings-driven,
        # exactly as before.
        registry.register(ExecuteShell(
            workspace,
            image=settings.agent_sandbox_image,
            memory_limit=settings.agent_sandbox_memory_limit,
            cpu_limit=settings.agent_sandbox_cpu_limit,
            pids_limit=settings.agent_sandbox_pids_limit,
            max_timeout_seconds=60,
        ))
        result: ToolResult = await registry.call(name, args)
        if result.success:
            return True, result.output
        return False, result.error

    if name in _BROWSER_TOOL_NAMES:
        if browser_session is None:
            return False, "Browser session not available"
        registry = ToolRegistry()
        registry.register(BrowserNavigate(browser_session))
        registry.register(BrowserGetText(browser_session))
        registry.register(BrowserScreenshot(workspace, browser_session))
        registry.register(BrowserClick(browser_session))
        registry.register(BrowserFill(browser_session))
        result: ToolResult = await registry.call(name, args)
        if result.success:
            return True, result.output
        return False, result.error

    if name in DESKTOP_TOOL_NAMES:
        if desktop_sandbox is None:
            return False, "Computer use is not available for this session"
        registry = ToolRegistry()
        registry.register(DesktopScreenshot(workspace, desktop_sandbox))
        registry.register(DesktopClick(workspace, desktop_sandbox))
        registry.register(DesktopDoubleClick(workspace, desktop_sandbox))
        registry.register(DesktopRightClick(workspace, desktop_sandbox))
        registry.register(DesktopType(workspace, desktop_sandbox))
        registry.register(DesktopKey(workspace, desktop_sandbox))
        registry.register(DesktopScroll(workspace, desktop_sandbox))
        registry.register(DesktopDrag(workspace, desktop_sandbox))
        registry.register(DesktopMove(workspace, desktop_sandbox))
        registry.register(DesktopOpen(workspace, desktop_sandbox))
        registry.register(DesktopGetWindows(workspace, desktop_sandbox))
        result: ToolResult = await registry.call(name, args)
        if result.success:
            return True, result.output
        return False, result.error

    return False, f"Unknown tool: {name!r}"


_ROLE_PT = {"link": "link", "button": "botão", "field": "campo", "element": "elemento"}
_TAG_ROLE = {"a": "link", "button": "button", "input": "field", "textarea": "field", "select": "field"}


def _page_target(selector: str, element: dict | None, default_role: str = "element") -> str:
    """The element an approval is about, in words: its accessible name when the page gave one, otherwise what the
    selector itself says in plain terms. Never the raw selector — "'a'" means nothing to the person approving."""
    selector = (selector or "").strip()
    if element and element.get("name"):
        role = _ROLE_PT.get(element.get("role", ""), "elemento")
        return f"no {role} “{element['name']}”"
    text = _re.match(r"^(?:text=|:?text\(|:has-text\()\s*['\"]?(.+?)['\"]?\s*\)?$", selector)
    if text:
        return f"no elemento com o texto “{text.group(1)}”"
    tag_role = _TAG_ROLE.get(selector.lower())
    if tag_role:
        return f"no primeiro {_ROLE_PT[tag_role]} da página"
    # A selector that names its element (#senha, button#comprar, input[name=email], [aria-label="Buscar"]) is read
    # as that name — what the page author called it, so nothing is invented. Anything more structural is not.
    named = _re.fullmatch(
        r"(?P<tag>[a-z]+)?(?:#(?P<id>[\w-]+)|\[(?:name|aria-label|placeholder|title)=['\"]?(?P<attr>[^'\"\]]+)['\"]?\])",
        selector, flags=_re.IGNORECASE,
    )
    if named:
        word = (named.group("id") or named.group("attr") or "").replace("-", " ").replace("_", " ").strip()
        role = _ROLE_PT.get(_TAG_ROLE.get((named.group("tag") or "").lower(), (element or {}).get("role") or default_role), "elemento")
        if word:
            return f"no {role} “{word}”"
    role = _ROLE_PT.get((element or {}).get("role") or default_role, "elemento")
    return f"em um {role} da página"


def _describe_tool_action(tool_name: str, args: dict, element: dict | None = None) -> str:
    """Plain-language description of a CONFIRM-level tool call for the
    approval gate — mirrors work_service.py's own _describe_confirm_action.

    `element` is what the open page says about the target of a browser action (BrowserSession.describe):
    its role and accessible name, used instead of the selector whenever it is there."""
    if tool_name == "write_file":
        path = args.get("path", "")
        return f"Escrever no arquivo '{path}'"
    if tool_name == "execute_shell":
        command = args.get("command", "")[:100]
        return f"Executar comando no sandbox: {command}"
    if tool_name == "browser_click":
        return f"Clicar {_page_target(str(args.get('selector', '')), element)}"
    if tool_name == "browser_fill":
        # The field is named so the user knows WHERE the agent is typing.
        # The value is not: it may be a password, a token or personal data,
        # and this description is rendered in the UI and written to the
        # activity stream.
        target = _page_target(str(args.get("selector", "")), element, default_role="field")
        return "Preencher " + target.replace("no ", "o ", 1).replace("em um ", "um ", 1)
    # Computer use. The user is approving an action on a screen they cannot
    # see from here, so the description says what will happen and where —
    # never just the tool name.
    if tool_name == "desktop_click":
        return f"Clicar em ({args.get('x')}, {args.get('y')}) no desktop virtual"
    if tool_name == "desktop_double_click":
        return f"Duplo clique em ({args.get('x')}, {args.get('y')}) no desktop virtual"
    if tool_name == "desktop_right_click":
        return f"Clique direito em ({args.get('x')}, {args.get('y')}) no desktop virtual"
    if tool_name == "desktop_type":
        # The text itself is withheld for the same reason browser_fill
        # withholds its value: it can carry a password or personal data, and
        # this string is rendered in the UI and written to the activity trail.
        return f"Digitar {len(str(args.get('text', '')))} caracteres no desktop virtual"
    if tool_name == "desktop_key":
        keys = args.get("keys") or []
        return f"Pressionar {'+'.join(str(k) for k in keys)} no desktop virtual"
    if tool_name == "desktop_scroll":
        return f"Rolar {args.get('clicks')} no desktop virtual"
    if tool_name == "desktop_drag":
        return (
            f"Arrastar de ({args.get('start_x')}, {args.get('start_y')}) "
            f"para ({args.get('end_x')}, {args.get('end_y')}) no desktop virtual"
        )
    if tool_name == "desktop_move":
        return f"Mover o cursor para ({args.get('x')}, {args.get('y')}) no desktop virtual"
    if tool_name == "desktop_open":
        return f"Abrir '{args.get('path', '')}' no desktop virtual"
    if tool_name == "desktop_screenshot":
        return "Capturar a tela do desktop virtual"
    if tool_name == "desktop_get_windows":
        return "Listar as janelas abertas no desktop virtual"
    return f"Executar ação: {tool_name}"


def _persist_user_message(db: DBSession, session_id: str, user_content: str) -> Message:
    """Synchronous DB work — called via asyncio.to_thread() so it runs off
    the event loop instead of stalling every other in-flight request for
    its duration (Etapa 5A; same rationale as chat_service.py)."""
    user_msg = Message(session_id=session_id, role="user", content=user_content)
    db.add(user_msg)
    db.commit()
    return user_msg


def _persist_coming_soon_reply(db: DBSession, session_id: str, coming_soon: str) -> Message:
    assistant_msg = Message(session_id=session_id, role="assistant", content=coming_soon)
    db.add(assistant_msg)
    db.commit()
    return assistant_msg


#: Plans whose answers built on web results must come from a model of at least this quality class. Production,
#: 2026-09-28: a MAX answer about "the latest" was written by gpt-4o-mini ("low") from a year-old page while a
#: fresher search sat unused in its own context. PRO/START keep their own floor: the plan decides what it pays for.
RESEARCH_QUALITY_FLOOR = {"max": "standard", "team": "standard"}


def research_routing(capability: str, plan: str, researched: bool) -> tuple[str, str | None]:
    """(capability, quality floor) for the next agent call.

    Before any web tool has run, the request routes as it was classified. After, the model is reading and
    summarising live results: that is research — which keeps the free pool out (free_pool.precision_reason) —
    and, on the plans listed above, it is also held to a quality floor the router can raise but never past the
    plan's own ceiling.
    """
    if not researched:
        return capability, None
    return "research", RESEARCH_QUALITY_FLOOR.get((plan or "").lower())


def _persist_media_reply(db: DBSession, session_id: str, reply: str, media_job_id: str | None) -> Message:
    """TRINDATRON's reply to a creation request, linked to the job so history reopens with the creation."""
    assistant_msg = Message(session_id=session_id, role="assistant", content=reply, media_job_id=media_job_id)
    db.add(assistant_msg)
    db.commit()
    return assistant_msg


def _session_owner(db: DBSession, user_id: str):
    from src.models.user import User

    return db.query(User).filter(User.id == user_id).one()


def _prepare_agent_context(
    db: DBSession,
    session: Session,
    user_content: str = "",
    task_type: str = "",
    capability: str = "",
    client_locale: str = "",
) -> tuple[list[dict], list[dict]]:
    """Returns (messages, active_tools). Synchronous DB work — includes any
    lazy-loaded session.project/session.agent_definition/skill relationships
    (real implicit queries, not just attribute access). Same to_thread
    rationale as chat_service.py's _prepare_chat_context.
    """
    history = (
        db.query(Message)
        .filter(Message.session_id == session.id)
        .order_by(Message.created_at)
        .all()
    )
    # Single source of truth for behaviour — see src/prompting/. The tool
    # policy is derived from the SAME schema list handed to the provider
    # (active_tools below), so what the model is told it has and what it can
    # actually call cannot drift apart.
    active_tools = _AGENT_TOOLS
    if session.agent_definition_id and session.agent_definition:
        agent = session.agent_definition
        if agent.skill_ids:
            skills = load_skills_for_context(db, agent.skill_ids, session.user_id)
            enabled_tool_ids: set[str] = set()
            for skill in skills:
                enabled_tool_ids.update(skill.tool_ids or [])
            enabled_tool_ids &= ALLOWED_TOOL_IDS
            active_tools = [t for t in _AGENT_TOOLS if t["function"]["name"] in enabled_tool_ids]

    # Plugins the person switched off in Settings (src/integrations/catalog.py). Removed from the schema
    # list itself, so the model is neither told about them nor able to call them. Only ever narrows.
    from src.integrations.catalog import disabled_tool_names
    from src.services import preferences_service

    switched_off = disabled_tool_names(preferences_service.disabled_plugins(db, session.user_id))
    if switched_off:
        active_tools = [t for t in active_tools if t["function"]["name"] not in switched_off]

    assembled = assemble(
        task_context=build_task_context(
            db, session, task_type=task_type, capability=capability, has_tools=bool(active_tools),
            client_locale=client_locale,
        ),
        tool_policy=from_tool_schemas(
            active_tools, approval_required=APPROVAL_REQUIRED_TOOL_NAMES
        ),
        memory=build_relevant_memory(db, session.user_id, user_content),
    )
    messages = [assembled.as_message()]
    # Availability comes from the <capabilities> block above, never from what an earlier reply said about it.
    messages += conversation_context(history)
    return messages, active_tools


#: The answer when the person declines an action that needed their approval (or the approval times out).
_DECLINED = {
    "pt": "Tudo bem, não fiz essa ação. Se quiser, me diga como prefere continuar.",
    "en": "All right, I didn't do that. Tell me how you'd like to continue.",
    "es": "De acuerdo, no hice esa acción. Dime cómo prefieres continuar.",
}


def _persist_agent_result(
    db: DBSession,
    session: Session,
    user_content: str,
    final_answer: str,
    final_model: str,
    reservation_id: str | None,
    total_input_tokens: int,
    total_output_tokens: int,
    total_cached_tokens: int,
    total_cost_usd: float,
    total_tcu: float,
    duration_ms: int,
    have_routing_result: bool,
) -> Message:
    """Synchronous DB work for stream_agent()'s post-loop persistence — exact
    same reserve-before/finalize-after sequencing as before this change,
    just relocated to run off the event loop (Etapa 5A)."""
    assistant_msg = Message(
        session_id=session.id,
        role="assistant",
        content=final_answer,
        model_used=final_model,
    )
    db.add(assistant_msg)
    db.commit()
    db.refresh(assistant_msg)

    if session.title == "New conversation" and user_content:
        session.title = (user_content[:60] + "…") if len(user_content) > 60 else user_content
        db.commit()

    if reservation_id:
        finalize_usage(
            db,
            reservation_id,
            message_id=assistant_msg.id,
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
            user_id=session.user_id,
            session_id=session.id,
            message_id=assistant_msg.id,
            model=final_model,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cached_tokens=total_cached_tokens if have_routing_result else 0,
            cost_usd=total_cost_usd,
            tcu=total_tcu,
            duration_ms=duration_ms,
        )
    return assistant_msg


async def stream_agent(
    db: DBSession,
    session: Session,
    user_content: str,
    user_plan: str | None = None,
    reservation_id: str | None = None,
    user_timezone: str | None = None,
    capability: Capability | None = None,
    request_control_token: RequestControlToken | None = None,
    agent_scheduler_token: AgentExecutionToken | None = None,
    forced_provider_key: str | None = None,
    continuity_execution_id: str | None = None,
    continuity_estimated_tcu: float = 0.0,
    idempotency_claim_id: str | None = None,
    agent_execution_id: str | None = None,
    client_locale: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    forced_provider_key/continuity_execution_id/continuity_estimated_tcu
    (Etapa 5F, execution wiring) — same contract as chat_service.
    stream_chat()'s identical parameters; see that function's docstring
    for the full rationale. Additionally, for the Agent/ReAct path
    specifically: when continuity_execution_id is set, browser_navigate/
    browser_get_text/browser_screenshot are removed from active_tools
    below — browser automation is exactly the "ambiguous or requires
    multimodality" case the Etapa 5F correction says to fail closed on
    (browser_screenshot unambiguously produces an image; the other two
    are excluded alongside it rather than left in a partially-usable
    state, per that same fail-closed instruction). Every other tool
    (read_file/write_file/list_directory/execute_shell/web_search) is
    text-in/text-out and stays available — Continuity does not mean
    "disable all of Agent", only "disable what's multimodal or unclear".

    idempotency_claim_id (Etapa 5I) — same contract as chat_service.
    stream_chat()'s identical parameter: set ONLY when the client sent an
    Idempotency-Key header AND claim_idempotency_key() returned a fresh
    claim (never a replay).

    user_timezone (Beta hardening, 2026-09-10) — current_user.timezone,
    forwarded from sessions.py exactly like check_and_reserve()'s own
    identical parameter, so the per-iteration TCU budget recheck below
    (check_agent_iteration_budget()) computes the SAME weekly window the
    upfront reservation used, never the UTC fallback. Only meaningful
    when reservation_id is set (Continuity Mode never has one — see
    below — so it never reaches the recheck at all).

    agent_execution_id (Etapa 5I) — set by sessions.py for EVERY /agent
    execution regardless of Idempotency-Key (unconditional lifecycle
    tracking, see agent_execution_service.py's module docstring). Flipped
    to "running" as the first statement below, then to exactly one of
    "completed"/"failed"/"interrupted" in this function's own `finally` —
    "interrupted" specifically for GeneratorExit/CancelledError (client
    disconnect, or with_execution_timeout() cutting this off), which is
    distinguishable from a handled "failed" exception because Python's
    `except Exception:` blocks below never catch those two (they are
    BaseException, not Exception) — see the finally block for exactly how
    that distinction is made via sys.exc_info().
    """
    workspace = _workspace_for(session.id)
    # Exclusive to this stream_agent() execution — never a module-level
    # singleton, never shared across sessions. Lazy: no Chromium process is
    # launched until a browser tool is actually called. `_browser_governor`
    # IS shared (process-wide, on purpose) — it only bounds total concurrent
    # Chromium processes, it holds no session/page state of its own.
    browser_session = BrowserSession(
        session.id,
        governor=_browser_governor,
        capacity_wait_timeout_s=settings.agent_browser_capacity_wait_timeout_s,
        shutdown_timeout_s=settings.agent_browser_shutdown_timeout_s,
    )
    # Computer use. Exclusive to this execution, and deliberately NOT started
    # here: a desktop container costs a few seconds and half a gigabyte, and
    # most agent runs never touch it. It is created on the first desktop tool
    # call and torn down in this function's finally — same lifetime shape as
    # the browser session above, same reason.
    _desktop_sandbox: DesktopSandbox | None = None
    _desktop_started = False
    resolved_plan = (user_plan or settings.default_user_plan).lower()
    # Connection-retention fix: captured once, before the read-only-txn
    # commit below, which (SQLAlchemy's default expire_on_commit=True)
    # marks every loaded attribute of `session` for reload on next access.
    # Every reference inside the ReAct loop (and its exception/finally
    # handling) uses these plain values instead of re-touching `session`,
    # which would otherwise silently reissue a SELECT (and re-acquire a
    # pool connection) on the FIRST such access, and then keep that
    # connection checked out for the rest of the loop — nothing inside it
    # ever commits again.
    _session_id = session.id
    _session_user_id = session.user_id

    # Track finalization state for cleanup in finally (handles GeneratorExit too).
    _cancel_on_exit = reservation_id is not None
    # Continuity true-up default — see stream_chat()'s identical comment:
    # full refund of the estimate unless success updates this below.
    continuity_actual_tcu = 0.0

    try:
        # Etapa 5I — "admitted" (set by sessions.py before this generator
        # was ever constructed) -> "running" the instant this body
        # actually starts executing (a generator's own code doesn't run
        # until the first __anext__()).
        await asyncio.to_thread(mark_execution_running, db, agent_execution_id)

        if capability is None:
            capability = classify(user_content)
        task_type = get_task_type(capability)
        yield capability_status_event(capability)

        user_msg = await asyncio.to_thread(_persist_user_message, db, session.id, user_content)

        # ── Questions about TRINDATRON itself — services/product_answers.py ──
        # What it runs on, and what it can do right now (from the capability
        # matrix), are answered by TRINDATRON before any model or tool runs, so
        # the reservation is released and the execution completes with no usage.
        from src.services import preferences_service

        answer_language = await asyncio.to_thread(
            preferences_service.answer_language, db, session.user_id, client_locale,
        )
        reply = await product_answer(db, session.user_id, user_content, answer_language or None)
        if reply is not None:
            assistant_msg = await asyncio.to_thread(
                _persist_coming_soon_reply, db, session.id, reply,
            )
            _cancel_on_exit = False
            if reservation_id:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
            await asyncio.to_thread(
                mark_idempotency_completed, db, idempotency_claim_id,
                session_id=session.id, message_id=assistant_msg.id,
            )
            await asyncio.to_thread(
                mark_execution_completed, db, agent_execution_id,
                message_id=assistant_msg.id, iterations_done=0,
            )
            yield f"data: {json.dumps({'type': 'done', 'session_id': session.id, 'message_id': assistant_msg.id, 'user_message_id': user_msg.id, 'answer': reply, 'iterations': 0, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'duration_ms': 0}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        # ── Creations: the same real media job as the conversation ───────────
        # An image or video asked for in agent mode is created, not refused and
        # not described by a model. The job reserves its own cost, sized to the
        # engine that runs, so this execution's reservation is released.
        if capability in (Capability.IMAGE, Capability.VIDEO):
            from src.services import chat_media

            _cancel_on_exit = False
            if reservation_id:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
            person = await asyncio.to_thread(_session_owner, db, _session_user_id)
            kind = "image" if capability is Capability.IMAGE else "video"
            reply, media_job = await chat_media.start_from_chat(db, person, user_content, kind, session_id=_session_id)
            if media_job is not None:
                yield f"data: {json.dumps({'type': 'artifact', 'artifact': chat_media.artifact_event(db, media_job)})}\n\n"
            assistant_msg = await asyncio.to_thread(
                _persist_media_reply, db, _session_id, reply, media_job.id if media_job is not None else None,
            )
            await asyncio.to_thread(
                mark_idempotency_completed, db, idempotency_claim_id,
                session_id=_session_id, message_id=assistant_msg.id,
            )
            await asyncio.to_thread(
                mark_execution_completed, db, agent_execution_id,
                message_id=assistant_msg.id, iterations_done=0,
            )
            done = {'type': 'done', 'session_id': _session_id, 'message_id': assistant_msg.id,
                    'user_message_id': user_msg.id, 'answer': reply, 'iterations': 0,
                    'usage': {'input_tokens': 0, 'output_tokens': 0, 'duration_ms': 0}}
            if media_job is not None:
                done['media_job_id'] = media_job.id
            yield f"data: {json.dumps(done)}\n\n"
            yield "data: [DONE]\n\n"
            return

        # ── Capability TRINDATRON does not have ──────────────────────────────
        if capability in UNIMPLEMENTED_CAPABILITIES:
            coming_soon = COMING_SOON_MESSAGES.get(
                capability,
                "Isso ainda não é uma capacidade do TRINDATRON.",
            )
            assistant_msg = await asyncio.to_thread(
                _persist_coming_soon_reply, db, session.id, coming_soon,
            )
            _cancel_on_exit = False
            # Nothing ran: the reservation is released now rather than left for the reclaim sweep.
            if reservation_id:
                await asyncio.to_thread(cancel_reservation, db, reservation_id)
            await asyncio.to_thread(
                mark_idempotency_completed, db, idempotency_claim_id,
                session_id=session.id, message_id=assistant_msg.id,
            )
            await asyncio.to_thread(
                mark_execution_completed, db, agent_execution_id,
                message_id=assistant_msg.id, iterations_done=0,
            )
            yield f"data: {json.dumps({'type': 'done', 'session_id': session.id, 'message_id': assistant_msg.id, 'user_message_id': user_msg.id, 'answer': coming_soon, 'iterations': 0, 'usage': {'input_tokens': 0, 'output_tokens': 0, 'duration_ms': 0}})}\n\n"
            yield "data: [DONE]\n\n"
            return

        messages, active_tools = await asyncio.to_thread(
            _prepare_agent_context, db, session, user_content,
            task_type.value, capability.value, client_locale or "",
        )
        # Etapa 5H — read once for the whole execution (same snapshot
        # convention as resolved_plan), reused across every ReAct
        # iteration below rather than re-queried per model call. A
        # mid-execution setting change takes effect on the NEXT
        # execution, never interrupts one already in progress — matching
        # the same "in-flight executions are not interrupted" guarantee
        # the Agent kill switch itself provides at the route-handler gate.
        disabled_provider_keys = await asyncio.to_thread(get_disabled_provider_keys, db)
        # Connection-retention fix: same rationale as chat_service.py's
        # identical call site. _prepare_agent_context's reads (history,
        # lazy-loaded project/agent/skills, memory context) and the read
        # above are the last DB touches before the ReAct loop below — no
        # `db` access happens anywhere inside that loop (provider calls and
        # tool execution are pure I/O, not persistence), so committing here
        # (a safe close of a read-only transaction — nothing was written
        # since _persist_user_message's own commit) releases the connection
        # BEFORE the loop starts and keeps it released for EVERY iteration,
        # not just the first, since nothing inside the loop reopens one.
        await asyncio.to_thread(db.commit)

        # Computer use is advertised to the model only when this request can
        # genuinely perform it: the plan includes it and the sandbox is
        # reachable. Offering a tool that will certainly fail wastes an
        # iteration, and — worse — puts a capability in front of the model
        # that the product cannot honour.
        if not await _desktop_available(resolved_plan):
            active_tools = [
                t for t in active_tools if t["function"]["name"] not in DESKTOP_TOOL_NAMES
            ]

        if continuity_execution_id is not None:
            # Browser and desktop tools removed for Continuity — see this
            # function's own docstring ("fail closed" on multimodal/ambiguous
            # tools). Continuity runs on a reduced budget without the user
            # present, which is the worst moment to start a container or
            # block on an approval nobody is there to give.
            active_tools = [
                t for t in active_tools
                if t["function"]["name"] not in _BROWSER_TOOL_NAMES
                and t["function"]["name"] not in DESKTOP_TOOL_NAMES
            ]

        final_answer = ""
        total_input_tokens = 0
        total_output_tokens = 0
        total_cached_tokens = 0
        total_cost_usd = 0.0
        total_tcu = 0.0
        iterations_done = 0
        routing_result = None
        start_time = time.time()

        # Root-cause fix — repeated-action circuit breaker (see
        # settings.agent_repeated_action_failure_limit's docstring).
        # Keyed by the exact (tool_name, args) signature so a DIFFERENT
        # error from the SAME action still counts as repetition — this
        # deliberately does NOT key on the error text, which could vary
        # between attempts even when the underlying action is identically
        # broken. `stopped_early_reason` short-circuits BOTH loops below
        # without disturbing the existing `for...else` exhaustion path
        # (that `else` only fires when the outer loop completes WITHOUT a
        # `break` — this circuit breaker always `break`s, so it never
        # collides with the pre-existing "ran out of iterations" message).
        _action_failure_counts: dict[str, int] = {}
        # Hardening (2026-09-02) — second, independent counter keyed on
        # tool_name ONLY (not args), catching a model that varies its
        # arguments slightly on every failing retry (never the same exact
        # signature twice, so _action_failure_counts above never trips).
        # Reset to 0 the instant that SAME tool succeeds; a different
        # tool's success does NOT reset another tool's streak. See
        # settings.agent_tool_failure_streak_limit's docstring.
        _tool_failure_streaks: dict[str, int] = {}
        stopped_early_reason: str | None = None
        # What this reservation was granted, read once. It cannot be re-read per call:
        # check_agent_iteration_budget rewrites the reservation's `tcu` to the running total (so the
        # usage sums stay true in real time), and the same column is what get_reservation_budget_tcu
        # returns. Re-reading it therefore handed call N+1 a ceiling equal to what call N had already
        # spent — near zero — so the router refused every candidate as over budget and a loop that
        # used a tool could never answer. Measured: a search returned 1,757 characters and the turn
        # ended with "não foi possível concluir".
        _granted_budget_tcu: float | None = None
        # Set once the loop has asked the live web: from then on the model is reading and summarising results,
        # which is research whatever the request was classified as — see research_routing().
        _researched = False

        try:
            for iteration in range(settings.agent_max_iterations):
                iterations_done = iteration + 1

                # What is left of the grant, never the whole grant again: the loop spends as it goes,
                # so each call may cost at most what has not been spent yet. This is the same bound
                # re-reading the reservation was reaching for, computed from the figure that still
                # means "ceiling" rather than from the one that now means "spent".
                _call_budget_usd: float | None = None
                if reservation_id:
                    if _granted_budget_tcu is None:
                        _granted_budget_tcu = await asyncio.to_thread(
                            get_reservation_budget_tcu, db, reservation_id
                        )
                        # Connection-retention fix, second gap: this bare read
                        # (no trailing commit/rollback) only runs on the FIRST
                        # iteration (cached into _granted_budget_tcu after), but
                        # that one time is enough to re-open a transaction —
                        # and re-check-out a pool connection — right before
                        # THIS iteration's provider call, undoing the loop-
                        # entry commit above for iteration 1 specifically.
                        # Nothing was written since, so this is the same safe
                        # close of a read-only transaction used everywhere else
                        # in this fix.
                        await asyncio.to_thread(db.commit)
                    _call_budget_usd = max(0.0, _granted_budget_tcu - total_tcu) / 100.0

                _route_capability, _quality_floor = research_routing(capability.value, resolved_plan, _researched)
                routing_result = await _router.complete(RoutingRequest(
                    messages=messages,
                    task_type=task_type,
                    user_plan=resolved_plan,
                    session_id=_session_id,
                    tools=active_tools,
                    tool_choice="auto",
                    capability=_route_capability,
                    min_quality_class=_quality_floor,
                    forced_provider_key=forced_provider_key,
                    disabled_provider_keys=disabled_provider_keys,
                    # See chat_service.py's identical note: the router may
                    # narrow its plan cap with this, never widen it.
                    max_cost_usd_override=_call_budget_usd,
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
                    if tool_name in WEB_RESEARCH_TOOLS:
                        _researched = True
                    try:
                        args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        args = {}

                    if continuity_execution_id is not None and (
                        tool_name in _BROWSER_TOOL_NAMES or tool_name in DESKTOP_TOOL_NAMES
                    ):
                        # Defense in depth — active_tools already excludes
                        # these from the model's function spec, so this
                        # should be unreachable; fail closed anyway rather
                        # than trust that alone.
                        logger.warning(
                            "continuity: blocked disallowed tool call | session=%s tool=%s execution_id=%s",
                            _session_id, tool_name, continuity_execution_id,
                        )
                        yield tool_status_event(tool_name)
                        yield f"data: {json.dumps({'type': 'tool_result', **tool_activity(tool_name), 'success': False, 'output': 'Esta ação não está disponível agora.'})}\n\n"
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": "ERROR: Tool not available in Continuity Mode.",
                        })
                        continue

                    # Security etapa — see this module's own
                    # _permission_gateway docstring above. Only consulted
                    # for a KNOWN tool name (ALLOWED_TOOL_IDS, the exact
                    # same canonical set skill_service.py already
                    # maintains) — an unrecognized tool_name (a model
                    # hallucination, or a genuinely-missing-from-the-
                    # manifest name) is a dispatch error, not a security
                    # decision, and must reach _run_tool()'s existing
                    # "Unknown tool: ..." handling immediately, exactly as
                    # before this etapa. PermissionGateway.level() itself
                    # defaults an unrecognized name to CONFIRM (a
                    # deliberately conservative choice for ITS original
                    # use case) — applying that here regardless of
                    # ALLOWED_TOOL_IDS would incorrectly pause the stream
                    # for 5 minutes over a name that was never going to
                    # execute anyway (caught by this etapa's own
                    # regression run: test_agent_scheduler_streaming.py's
                    # "nonexistent_tool" test).
                    #
                    # FORBIDDEN denies outright (no current AGENT tool is
                    # FORBIDDEN in DEFAULT_MANIFEST; defense in depth,
                    # mirrors the Continuity-blocked-tool check just
                    # above). CONFIRM pauses the SSE stream via the SAME
                    # async approval gate TRINITY WORK already uses
                    # (src.services.approval_gate), owned by this
                    # session_id — a different session can never resolve
                    # this gate, and the /agent/approve|reject routes
                    # independently re-check session ownership via the DB
                    # first too.
                    permission_level = (
                        _permission_gateway.level(tool_name)
                        if tool_name in ALLOWED_TOOL_IDS
                        else PermissionLevel.AUTO
                    )
                    if permission_level == PermissionLevel.FORBIDDEN:
                        logger.warning(
                            "agent: tool forbidden by policy | session=%s tool=%s",
                            _session_id, tool_name,
                        )
                        yield tool_status_event(tool_name)
                        yield f"data: {json.dumps({'type': 'tool_result', **tool_activity(tool_name), 'success': False, 'output': 'Esta ação não é permitida.'})}\n\n"
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id,
                            "content": "ERROR: Tool forbidden by policy.",
                        })
                        continue

                    if permission_level == PermissionLevel.CONFIRM:
                        action_id = str(_uuid_module.uuid4())
                        gate = create_gate(action_id, owner_id=_session_id)
                        # What the page calls the element, when the page is open: the prompt names it as the
                        # person sees it ("o link “More information”"), never by its selector ("'a'").
                        element = (
                            await browser_session.describe(str(args.get("selector", "")))
                            if tool_name in ("browser_click", "browser_fill") else None
                        )
                        description = _describe_tool_action(tool_name, args, element)
                        yield f"data: {json.dumps({'type': 'approval_required', 'action_id': action_id, **tool_activity(tool_name), 'args': args, 'description': description})}\n\n"
                        yield tool_status_event(tool_name)

                        logger.info(
                            "agent: awaiting approval | session=%s tool=%s action_id=%s",
                            _session_id, tool_name, action_id,
                        )
                        approved = await wait_for_gate(action_id, gate)
                        if not approved:
                            # Declining is the person's choice, not a failure: the turn ends like any other — its
                            # answer saved, the model calls already made charged at their real cost, and `done`
                            # sent. It used to end as an error: the app said "encontrou um erro", and the calls
                            # made before the approval were cancelled with the reservation instead of recorded.
                            yield f"data: {json.dumps({'type': 'tool_result', **tool_activity(tool_name), 'success': False, 'output': 'declined'})}\n\n"
                            stopped_early_reason = "approval_declined"
                            final_answer = _DECLINED.get(answer_language or "", _DECLINED["pt"])
                            break
                    else:
                        yield tool_status_event(tool_name)

                    yield f"data: {json.dumps({'type': 'tool_call', **tool_activity(tool_name)})}\n\n"

                    logger.info(
                        "agent tool_call | session=%s tool=%s args=%s",
                        _session_id, tool_name, json.dumps(args)[:200],
                    )

                    if tool_name in DESKTOP_TOOL_NAMES and not _desktop_started:
                        # First desktop action of this run: bring the virtual
                        # desktop up. A failure here is reported as a tool
                        # failure rather than raised — the model can carry on
                        # with the tools that do work, which is a better
                        # outcome than ending the whole run.
                        _desktop_sandbox = DesktopSandbox(
                            image=settings.agent_desktop_sandbox_image,
                            memory_limit=settings.agent_sandbox_memory_limit,
                            cpu_limit=settings.agent_sandbox_cpu_limit,
                            pids_limit=settings.agent_sandbox_pids_limit,
                        )
                        _desktop_started = await _desktop_sandbox.start(_session_id, workspace)
                        if not _desktop_started:
                            _desktop_sandbox = None
                            logger.warning(
                                "agent: desktop sandbox failed to start | session=%s", _session_id,
                            )

                    success, output = await _run_tool(
                        tool_name, args, workspace,
                        browser_session=browser_session,
                        desktop_sandbox=_desktop_sandbox,
                    )
                    truncated = output[:_MAX_OUTPUT_CHARS] if len(output) > _MAX_OUTPUT_CHARS else output

                    logger.info(
                        "agent tool_result | session=%s tool=%s success=%s output_len=%d",
                        _session_id, tool_name, success, len(output),
                    )

                    yield f"data: {json.dumps({'type': 'tool_result', **tool_activity(tool_name), 'success': success, 'output': truncated})}\n\n"

                    llm_content = truncated if success else f"ERROR: {truncated}"
                    if success and tool_name in _EXTERNAL_TOOLS:
                        llm_content = _wrap_external(tool_name, llm_content)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": llm_content,
                    })

                    if success:
                        # Genuine progress for THIS tool — a different
                        # tool succeeding does not clear another tool's
                        # streak (see _tool_failure_streaks' own comment
                        # above): only that tool's own success proves its
                        # own problem is resolved.
                        _tool_failure_streaks[tool_name] = 0
                    else:
                        action_signature = f"{tool_name}:{json.dumps(args, sort_keys=True)}"
                        failure_count = _action_failure_counts.get(action_signature, 0) + 1
                        _action_failure_counts[action_signature] = failure_count
                        if failure_count >= settings.agent_repeated_action_failure_limit:
                            logger.warning(
                                "agent: repeated-action circuit breaker tripped | session=%s tool=%s "
                                "failure_count=%d limit=%d",
                                _session_id, tool_name, failure_count,
                                settings.agent_repeated_action_failure_limit,
                            )
                            _agent_early_stops_total.inc(reason="repeated_action_failure")
                            stopped_early_reason = "repeated_action_failure"
                            final_answer = (
                                f"I stopped because the '{tool_name}' action failed "
                                f"{failure_count} times with the exact same input "
                                "without making progress. Retrying it again would not "
                                "help — please adjust the request or try a different "
                                "approach."
                            )
                            break

                        streak = _tool_failure_streaks.get(tool_name, 0) + 1
                        _tool_failure_streaks[tool_name] = streak
                        if streak >= settings.agent_tool_failure_streak_limit:
                            logger.warning(
                                "agent: tool no-progress circuit breaker tripped | session=%s tool=%s "
                                "streak=%d limit=%d",
                                _session_id, tool_name, streak,
                                settings.agent_tool_failure_streak_limit,
                            )
                            _agent_early_stops_total.inc(reason="tool_no_progress")
                            stopped_early_reason = "tool_no_progress"
                            final_answer = (
                                f"I stopped because the '{tool_name}' tool failed "
                                f"{streak} times in a row (with varying inputs) "
                                "without making progress. Retrying it again would not "
                                "help — please adjust the request or try a different "
                                "approach."
                            )
                            break

                # Beta hardening (2026-09-10) — AGENT OVERSHOOT guard. Reached
                # only when tool_calls were present (the loop is about to call
                # the model again) and no OTHER circuit breaker already
                # decided to stop. Reuses the SAME entitlement-service source
                # of truth check_and_reserve() itself uses — see
                # check_agent_iteration_budget()'s own docstring. Skipped
                # entirely when reservation_id is None (Continuity Mode,
                # which never reserves TCU this way — see this function's
                # own docstring).
                if stopped_early_reason is None and reservation_id:
                    reservation_ok = await asyncio.to_thread(
                        check_agent_iteration_budget,
                        db, reservation_id, _session_user_id, resolved_plan,
                        total_tcu, user_timezone,
                    )
                    if not reservation_ok:
                        logger.warning(
                            "agent: stopped due to TCU budget exceeded mid-loop | "
                            "session=%s iteration=%d total_tcu=%.4f",
                            _session_id, iterations_done, total_tcu,
                        )
                        _agent_early_stops_total.inc(reason="tcu_budget_exceeded")
                        stopped_early_reason = "tcu_budget_exceeded"
                        final_answer = (
                            "I stopped because continuing would exceed your available "
                            "TCU budget. The work completed so far has been saved."
                        )

                if stopped_early_reason is not None:
                    break
            else:
                final_answer = "I was unable to complete this task within the allowed number of steps."

        except QualityNotEntitledError as exc:
            # Entitlement/Quality Gate — the task genuinely requires a
            # quality tier this plan is not entitled to (e.g. 'premium'
            # on 'start'/'free'). Router already rejected this BEFORE
            # any candidate was considered or provider called — zero
            # cost incurred. Distinct, semantically correct error (not
            # the generic "AI service temporarily unavailable" a plain
            # ProviderUnavailableError would produce) so the client can
            # show an upgrade prompt instead of a transient-outage retry.
            yield f"data: {json.dumps({'type': 'error', 'error': 'quality_not_entitled', 'message': 'This request requires a capability level your current plan does not include. Upgrade your plan to access it.', 'required_quality_class': exc.required_quality_class, 'plan_max_quality_class': exc.plan_max_quality_class, 'plan': exc.plan})}\n\n"
            return
        except BudgetExceededError:
            yield f"data: {json.dumps(safe_error_payload('request_too_large'))}\n\n"
            return
        except ProviderUnavailableError:
            yield f"data: {json.dumps(safe_error_payload('temporarily_unavailable'))}\n\n"
            return
        except Exception:
            logger.exception("Unexpected error in agent loop | session=%s", _session_id)
            yield f"data: {json.dumps(safe_error_payload('internal'))}\n\n"
            return

        final_answer = redact_self_identification(final_answer)
        duration_ms = int((time.time() - start_time) * 1000)
        final_model = routing_result.model if routing_result is not None else ""

        assistant_msg = await asyncio.to_thread(
            _persist_agent_result,
            db, session, user_content, final_answer, final_model, reservation_id,
            total_input_tokens, total_output_tokens, total_cached_tokens,
            total_cost_usd, total_tcu, duration_ms, routing_result is not None,
        )

        _cancel_on_exit = False
        # Continuity true-up target — total_tcu already accumulates across
        # every ReAct iteration (see the loop above), exactly the "sum of
        # real usage across possibly several model calls" true_up_continuity()
        # needs to reconcile against the single upfront estimate.
        continuity_actual_tcu = total_tcu
        # Etapa 5I — same "only at the confirmed-success point" timing as
        # the reservation finalize above. No-op when the corresponding id
        # is None (no Idempotency-Key header sent / not an Agent path).
        await asyncio.to_thread(
            mark_idempotency_completed, db, idempotency_claim_id,
            session_id=session.id, message_id=assistant_msg.id,
        )
        await asyncio.to_thread(
            mark_execution_completed, db, agent_execution_id,
            message_id=assistant_msg.id, iterations_done=iterations_done,
        )

        logger.info(
            "agent completed | session=%s model=%s plan=%s iterations=%d "
            "tokens_in=%d tokens_out=%d cost_usd=%.6f tcu=%.4f duration_ms=%d",
            session.id, final_model, resolved_plan, iterations_done,
            total_input_tokens, total_output_tokens,
            total_cost_usd, total_tcu, duration_ms,
        )

        yield f"data: {json.dumps({'type': 'done', 'session_id': session.id, 'message_id': assistant_msg.id, 'user_message_id': user_msg.id, 'answer': final_answer, 'iterations': iterations_done, 'usage': {'input_tokens': total_input_tokens, 'output_tokens': total_output_tokens, 'duration_ms': duration_ms}})}\n\n"
        yield "data: [DONE]\n\n"

    finally:
        # Unconditional on every exit path — success, error, and
        # GeneratorExit (SSE client disconnect) all hit this finally. The
        # concurrency slot's lifetime is the whole stream, not the route
        # handler call that returned this generator — see
        # request_control.py's RequestControlToken docstring.
        if request_control_token is not None:
            request_control_token.release()
        if agent_scheduler_token is not None:
            agent_scheduler_token.release()
        await browser_session.close()
        if _desktop_sandbox is not None:
            # stop() is safe on a sandbox that never started, and this runs
            # on the GeneratorExit/CancelledError path too — a disconnected
            # client must not leave a container running.
            await _desktop_sandbox.stop()
        if _cancel_on_exit and reservation_id:
            await asyncio.to_thread(cancel_reservation, db, reservation_id)
        if _cancel_on_exit:
            # Etapa 5I — mirrors cancel_reservation's own "_cancel_on_exit"
            # condition exactly. A retry with the same Idempotency-Key is
            # then correctly treated as a brand-new attempt.
            await asyncio.to_thread(
                mark_idempotency_failed, db, idempotency_claim_id, reservation_id=reservation_id,
            )
            # Distinguish "interrupted" (GeneratorExit/CancelledError — the
            # client never received a clean SSE error event; this `finally`
            # is reached WITHOUT any of the `except Exception`/
            # `except BudgetExceededError`/`except ProviderUnavailableError`
            # blocks above having run, since those only catch Exception
            # subclasses, never the two BaseException-only interruption
            # signals) from "failed" (one of those blocks DID run and
            # already yielded a clean error event before this `finally`).
            exc_type, _exc_value, _exc_tb = sys.exc_info()
            if exc_type is not None and issubclass(exc_type, (GeneratorExit, asyncio.CancelledError)):
                await asyncio.to_thread(mark_execution_interrupted, db, agent_execution_id)
            else:
                await asyncio.to_thread(mark_execution_failed, db, agent_execution_id)
        if continuity_execution_id is not None:
            await asyncio.to_thread(
                true_up_continuity,
                db,
                user_id=_session_user_id,
                estimated_tcu=continuity_estimated_tcu,
                actual_tcu=continuity_actual_tcu,
                execution_id=continuity_execution_id,
            )
