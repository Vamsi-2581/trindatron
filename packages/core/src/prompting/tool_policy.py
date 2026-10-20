"""ToolPolicy — what the model may reach for, and what waits for a human.

Why this is a layer and not a paragraph in the policy
-----------------------------------------------------
The tool set is per-request: it varies by capability, by plan, by which skills
a session enabled, and by what is installed at all. The policy is a constant.
Writing tool names into the policy would mean either listing tools that are
absent from most requests — inviting the model to offer capabilities it does
not have — or rewriting the constant per request, which is not a constant.

What this layer is and is not
-----------------------------
It is a DESCRIPTION for the model of what is available and what is gated. It is
not the enforcement. The tool schemas passed to the provider decide what can be
called at all, and the approval gate decides what actually runs; both live in
code and neither consults this text. If the two ever disagree, the code wins
and this layer was simply wrong — which is why it is generated FROM the same
tool list rather than maintained beside it.

The honesty rule this makes possible
------------------------------------
Telling the model which tools require approval is what lets the policy's
execution block mean something: a model that knows an action is gated can say
"this needs your approval" instead of claiming it ran.
"""
from __future__ import annotations

from dataclasses import dataclass, field


#: Tools whose output is the live web. When any of them is attached, the answer is only as good as the way the
#: results are read — so the reading rules come with them.
WEB_RESEARCH_TOOLS = frozenset({"web_search", "browser_navigate", "browser_get_text"})

# Production, 2026-09-28: asked for "the latest electric car created in Brazil", the agent searched twice (once for
# 2026), then answered with a July 2025 launch as "the latest", figures with no source and no date, and no links.
#: Kept short on purpose: the system prompt has a soft budget (prompting/budget.py) and memory is the first section
#: dropped when it is exceeded — a longer version of these rules pushed an agent turn over it and lost the memory.
WEB_RESEARCH_RULES = (
    "Web results: date each source (page, snippet or URL) against <today>. For what is latest or current, prefer "
    "the newest source; if it is old, search again for the current year or say how old it is. Say when the "
    "information is from; give a figure only if a cited source has it. End with a \"Sources:\" list of the links "
    "used, dated when known."
)


@dataclass(frozen=True)
class ToolDescriptor:
    """One tool as the model should understand it."""

    name: str
    #: True when calling it pauses for a human decision. Sourced from the
    #: permission manifest, never hand-maintained here.
    requires_approval: bool = False


@dataclass(frozen=True)
class ToolPolicy:
    tools: tuple[ToolDescriptor, ...] = field(default_factory=tuple)

    @classmethod
    def none(cls) -> "ToolPolicy":
        """No tools in this request. Renders an explicit statement rather than
        nothing: a model told it has no tools answers directly, while a model
        told nothing sometimes narrates tool calls it cannot make."""
        return cls(tools=())

    def render(self) -> str:
        if not self.tools:
            return (
                "<tools>No tools are attached to this request: nothing can be searched, "
                "run or fetched in this reply, and answers come from what you know. That "
                "is this request's state, not a limit of TRINDATRON — if the task needs "
                "more, say what the <capabilities> block says about it.</tools>"
            )

        gated = [t.name for t in self.tools if t.requires_approval]
        lines = [
            "<tools>",
            "Available: " + ", ".join(t.name for t in self.tools) + ".",
            "These are the only tools that exist for this request.",
        ]
        if gated:
            lines.append(
                "Require the person's approval before they run: "
                + ", ".join(gated)
                + ". Requesting one of these pauses the task — report that it is "
                "waiting, never that it is done."
            )
        if any(t.name in WEB_RESEARCH_TOOLS for t in self.tools):
            lines.append(WEB_RESEARCH_RULES)
        lines.append("</tools>")
        return "\n".join(lines)

    @property
    def approx_tokens(self) -> int:
        return len(self.render()) // 4


def from_tool_schemas(
    schemas: list[dict],
    *,
    approval_required: frozenset[str] | None = None,
) -> ToolPolicy:
    """Build the layer from the SAME schemas handed to the provider.

    Derived rather than written by hand, so the description and the reality
    cannot drift: a tool added to the schema list appears here automatically,
    and one removed disappears. That property is what the "no forbidden tool is
    ever advertised" test actually asserts.
    """
    approval_required = approval_required or frozenset()
    descriptors: list[ToolDescriptor] = []
    for schema in schemas or []:
        name = (schema.get("function") or {}).get("name") if isinstance(schema, dict) else None
        if not name:
            continue
        descriptors.append(
            ToolDescriptor(name=name, requires_approval=name in approval_required)
        )
    return ToolPolicy(tools=tuple(descriptors))
