"""Graph builder — assemble nodes into a compiled StateGraph."""

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.knowledge import knowledge_node
from app.graph.nodes.responder import responder_node
from app.graph.nodes.router import DEFAULT_ROUTE, router_node
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP, tool_exec_node
from app.graph.state import IPAState
from app.graph.utils import get_last_message

# Routes without a specialist yet fall back to the responder.
ROUTE_MAP = {
    "knowledge": "knowledge",
    # time / weather / calendar / music / alarm / clarify -> responder (M04 stubs)
}


def route_edge(state: IPAState) -> str:
    """Map the router's decision to a node name. Unknown -> responder."""
    route = state.get("route") or DEFAULT_ROUTE
    return ROUTE_MAP.get(route, "responder")


def should_continue(state: IPAState) -> str:
    """Decide what happens after the specialist produces a message.

    - iteration cap hit -> force-exit to responder (budget guardrail)
    - last message has tool_calls -> run the tool dispatcher
    - otherwise -> specialist gave a final answer -> responder formats it
    """
    if state["tool_iterations"] >= TOOL_ITERATION_CAP:
        return "responder"
    last = get_last_message(state["messages"])

    if getattr(last, "tool_calls", None):
        return "tool_exec"
    return "responder"


def build_graph():
    builder = StateGraph(IPAState)
    builder.add_node("router", router_node)
    builder.add_node("knowledge", knowledge_node)
    builder.add_node("tool_exec", tool_exec_node)
    builder.add_node("responder", responder_node)

    builder.add_edge(START, "router")
    builder.add_conditional_edges(
        "router",
        route_edge,
        {"knowledge": "knowledge", "responder": "responder"},
    )
    builder.add_conditional_edges(
        "knowledge",
        should_continue,
        {"tool_exec": "tool_exec", "responder": "responder"},
    )
    builder.add_edge("tool_exec", "knowledge")
    builder.add_edge("responder", END)

    return builder.compile()
