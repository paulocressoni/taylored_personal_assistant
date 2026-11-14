"""Graph builder — assemble the single tool-using assistant."""

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from app.graph.nodes.assistant import assistant_node
from app.graph.nodes.detect_lang import detect_lang_node
from app.graph.nodes.finalize import finalize_node
from app.graph.nodes.telemetry import telemetry_node
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP, tool_exec_node
from app.graph.state import IPAState
from app.graph.utils import get_last_message


def should_continue(state: IPAState) -> Literal["tool_exec", "telemetry"]:
    """Decide whether the assistant's last message needs a tool round.

    Args:
        state: The current IPA state, including the latest message.

    Returns:
        ``tool_exec`` when the last message requested tools, else ``telemetry``.
    """
    last = get_last_message(state["messages"])
    return "tool_exec" if getattr(last, "tool_calls", None) else "telemetry"


def after_tools(state: IPAState) -> Literal["finalize", "assistant"]:
    """Decide whether the tool loop continues or wraps up.

    Args:
        state: The current IPA state, including the iteration count.

    Returns:
        ``finalize`` once the iteration cap is reached, else ``assistant``.
    """
    return "finalize" if state["tool_iterations"] >= TOOL_ITERATION_CAP else "assistant"


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> Any:
    """Assemble the nodes into a compiled, optionally check-pointed graph.

    Args:
        checkpointer: Optional LangGraph BaseCheckpointSaver. When given, the
            compiled graph persists its state to that store after every step
            and reloads it on the next run with the same thread_id (M11).
            When None, the graph is stateless (CLI / tests).

    Returns:
        The compiled StateGraph, ready to invoke/ainvoke.
    """
    builder = StateGraph(IPAState)
    builder.add_node("detect_lang", detect_lang_node)
    builder.add_node("assistant", assistant_node)
    builder.add_node("tool_exec", tool_exec_node)
    builder.add_node("finalize", finalize_node)
    builder.add_node("telemetry", telemetry_node)

    # Language first: the assistant reads a resolved state["lang"].
    builder.add_edge(START, "detect_lang")
    builder.add_edge("detect_lang", "assistant")
    builder.add_conditional_edges(
        "assistant",
        should_continue,
        {"tool_exec": "tool_exec", "telemetry": "telemetry"},
    )
    # No assistant -> finalize edge: the cap is only reached inside tool_exec,
    # and routing there with pending tool_calls would send a dangling
    # AIMessage(tool_calls) to a model. after_tools owns the cap decision.
    builder.add_conditional_edges(
        "tool_exec",
        after_tools,
        {"finalize": "finalize", "assistant": "assistant"},
    )
    builder.add_edge("finalize", "telemetry")
    # telemetry runs LAST so it can reconcile the run totals and persist mode.
    builder.add_edge("telemetry", END)

    return builder.compile(checkpointer=checkpointer)
