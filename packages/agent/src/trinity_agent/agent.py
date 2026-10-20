from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import AsyncIterator

from trinity_router import (
    BudgetExceededError,
    ModelRouter,
    ProviderUnavailableError,
    RoutingRequest,
    TaskType,
)

from trinity_agent.permissions.gateway import PermissionGateway, PermissionDenied
from trinity_agent.security.audit import AuditLog, AuditEntry
from trinity_agent.security.sanitizer import wrap_external_content
from trinity_agent.tools.registry import ToolRegistry

_router = ModelRouter()

SYSTEM_PROMPT = """\
You are TRINITY AGENT, an autonomous AI assistant that can use tools to help users.
You operate in a ReAct loop: think through the problem, choose a tool, observe the result, repeat.
When you have enough information, respond with your final answer WITHOUT calling any tool.
Always be concise and accurate. Never fabricate tool results.
"""


@dataclass
class AgentResult:
    answer: str
    iterations: int
    tool_calls: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    tcu: float = 0.0
    error: str = ""


class TrinityAgent:
    def __init__(
        self,
        registry: ToolRegistry,
        gateway: PermissionGateway,
        audit_log: AuditLog | None = None,
        max_iterations: int = 10,
        session_id: str = "",
        user_plan: str = "start",
    ):
        self._registry = registry
        self._gateway = gateway
        self._audit = audit_log or AuditLog()
        self._max_iterations = max_iterations
        self._session_id = session_id
        self._user_plan = user_plan

    async def run(self, user_message: str) -> AgentResult:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        tools = self._registry.litellm_specs()
        tool_call_log: list[dict] = []
        total_cost_usd = 0.0
        total_tcu = 0.0

        for iteration in range(self._max_iterations):
            try:
                routing_result = await _router.complete(RoutingRequest(
                    messages=messages,
                    task_type=TaskType.AGENT,
                    user_plan=self._user_plan,
                    session_id=self._session_id,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                ))
            except BudgetExceededError as exc:
                return AgentResult(
                    answer="",
                    iterations=iteration + 1,
                    tool_calls=tool_call_log,
                    cost_usd=total_cost_usd,
                    tcu=total_tcu,
                    error=str(exc),
                )
            except ProviderUnavailableError:
                return AgentResult(
                    answer="",
                    iterations=iteration + 1,
                    tool_calls=tool_call_log,
                    cost_usd=total_cost_usd,
                    tcu=total_tcu,
                    error="ai_provider_unavailable",
                )

            total_cost_usd += routing_result.cost_usd
            total_tcu += routing_result.tcu

            if not routing_result.tool_calls:
                return AgentResult(
                    answer=routing_result.content or "",
                    iterations=iteration + 1,
                    tool_calls=tool_call_log,
                    cost_usd=total_cost_usd,
                    tcu=total_tcu,
                )

            messages.append({
                "role": "assistant",
                "content": routing_result.content,
                "tool_calls": routing_result.tool_calls,
            })

            for tc in routing_result.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                try:
                    self._gateway.check(tool_name, args)
                except PermissionDenied as e:
                    tool_result_content = f"PERMISSION DENIED: {e}"
                    result_success = False
                else:
                    result = await self._registry.call(tool_name, args)
                    tool_result_content = result.to_llm_str()
                    result_success = result.success

                    if _is_external_source(tool_name):
                        tool_result_content = wrap_external_content(tool_name, tool_result_content)

                self._audit.record(AuditEntry.create(
                    tool_name=tool_name,
                    args=args,
                    result=tool_result_content[:500],
                    permission_level=str(self._gateway.level(tool_name)),
                    session_id=self._session_id,
                ))
                tool_call_log.append({"tool": tool_name, "args": args, "success": result_success})

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result_content,
                })

        return AgentResult(
            answer="Maximum iterations reached without a final answer.",
            iterations=self._max_iterations,
            tool_calls=tool_call_log,
            cost_usd=total_cost_usd,
            tcu=total_tcu,
            error="max_iterations_exceeded",
        )


def _is_external_source(tool_name: str) -> bool:
    return tool_name in {"web_search", "browser_get_text", "browser_navigate", "read_file"}
