"""SendNotification / ScheduleAction — approval-gated action STUBS.

Honesty note (found during the Tool/Connector Foundation audit): neither
tool performs any real external action today. No message is actually
sent; no action is actually scheduled. Before this etapa, TRINITY WORK's
approval-gate branch (src/services/work_service.py) synthesized the SAME
canned "Action approved by user. Executed {tool} successfully." text
directly in the ReAct loop, WITHOUT ever calling any function for the
claimed action — these two run() bodies below simply make that existing,
pre-existing stub behavior an honest, declared property of the tool
itself (requires_approval=True) instead of a separately-maintained
_CONFIRM_TOOLS name set. When a real Connector (email/calendar/messaging)
backs these, run() should call it — nothing here should be mistaken for
that already existing.
"""
from __future__ import annotations

from .base import BaseTool, SideEffect, ToolResult


class SendNotification(BaseTool):
    name = "send_notification"
    description = "Send a notification or message to an external destination (requires user approval)."
    side_effect = SideEffect.EXTERNAL_SIDE_EFFECT
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "destination": {"type": "string", "description": "Recipient or channel"},
            "message": {"type": "string", "description": "Message content"},
        },
        "required": ["destination", "message"],
    }

    async def run(self, **_) -> ToolResult:
        return ToolResult(success=True, output=f"Action approved by user. Executed {self.name} successfully.")


class ScheduleAction(BaseTool):
    name = "schedule_action"
    description = "Schedule a future action (requires user approval)."
    side_effect = SideEffect.EXTERNAL_SIDE_EFFECT
    requires_approval = True
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Description of the action"},
            "when": {"type": "string", "description": "When to execute (e.g. 'tomorrow 9am')"},
        },
        "required": ["action", "when"],
    }

    async def run(self, **_) -> ToolResult:
        return ToolResult(success=True, output=f"Action approved by user. Executed {self.name} successfully.")
