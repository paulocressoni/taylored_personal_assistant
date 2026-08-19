"""Central tool registry — the single list specialists bind and tool_exec
dispatches against. Add every new tool here; nothing else changes."""

from app.tools.calculator import calculate

TOOLS = [calculate]
tools_by_name = {tool_.name: tool_ for tool_ in TOOLS}
