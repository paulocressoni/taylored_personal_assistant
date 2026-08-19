"""tool_exec_node — GENERIC tool dispatcher.

Looks each tool_call's name up in tools_by_name and invokes it. It knows
NOTHING about what the tools do — it only knows how to run a registry.
Every exception from a tool is captured and turned into a ToolMessage so a
tool failure can never crash the whole graph.
"""

from langchain_core.messages import SystemMessage, ToolMessage

from app.graph.nodes.knowledge import tools_by_name
from app.graph.state import IPAState
from app.graph.utils import get_last_message

# Hard cap on ReAct loop iterations. Without this, a tool that keeps saying
# "I need more information" would spin forever and burn real API budget.
TOOL_ITERATION_CAP = 5


def tool_exec_node(state: IPAState) -> dict:
    last = get_last_message(state["messages"])
    tool_calls = getattr(last, "tool_calls", None) or []

    results: list[ToolMessage] = []
    for call in tool_calls:
        name = call["name"]
        args = call.get("args") or {}
        tool = tools_by_name.get(name)

        if tool is None:
            content = f"Unknown tool: {name}"
        else:
            try:
                content = tool.invoke(args)
            except Exception as exc:  # never let a tool crash the graph
                content = f"Tool error: {type(exc).__name__}: {exc}"

        results.append(ToolMessage(content=str(content), tool_call_id=call["id"]))

    iterations = state["tool_iterations"] + 1
    out: dict = {"messages": results, "tool_iterations": iterations}

    # Cap reached: append a SystemMessage that tells the responder to stop
    # and explain that the tool loop was cut off — the "force-exit with an
    # explanatory message".
    if iterations >= TOOL_ITERATION_CAP:
        out["messages"] = results + [
            SystemMessage(
                content=(
                    "The tool loop reached its iteration cap and was stopped. "
                    "Do NOT call any more tools. Tell the user you couldn't "
                    "get a definitive answer and suggest rephrasing."
                )
            )
        ]
    return out
