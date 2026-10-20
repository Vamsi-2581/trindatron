from __future__ import annotations

from .base import BaseTool, SideEffect, ToolResult


class CreateDocument(BaseTool):
    """Composes a formatted text document as tool output. No filesystem
    or external write occurs — the client may render the returned text,
    but nothing outside this one response is changed, so this is
    READ_ONLY like every other pure-composition tool here."""
    name = "create_document"
    description = "Create a text document with a title and content."
    side_effect = SideEffect.READ_ONLY
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Document title"},
            "content": {"type": "string", "description": "Document content (markdown)"},
        },
        "required": ["title", "content"],
    }

    async def run(self, title: str = "Untitled", content: str = "", **_) -> ToolResult:
        return ToolResult(success=True, output=f"Document created: **{title}**\n\n{content}")
