"""Graph builder — assemble nodes into a compiled StateGraph."""

from langgraph.graph import END, START, StateGraph

from app.graph.nodes.responder import responder_node
from app.graph.state import IPAState


def build_graph():
    builder = StateGraph(IPAState)
    builder.add_node("responder", responder_node)
    builder.add_edge(START, "responder")
    builder.add_edge("responder", END)
    return builder.compile()
