"""Unit tests for graph branching + the generic tool dispatcher.

should_continue and tool_exec_node are PURE graph logic: they never touch the
LLM, so NO mocking is needed — just well-formed state dicts. This is the
cheapest, fastest layer of M06's three-tier strategy.

The one seam: tool_exec_node looks tools up in tools_by_name (a name imported
into its module). To prove exception-wrapping we monkeypatch that registry
with a tool that raises — the same dependency-injection idea as the LLM fake.
"""

import pytest
from conftest import make_state
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool

from app.graph.graph import route_edge, should_continue
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP, tool_exec_node


def _tool_calls(*calls: tuple[str, dict, str]) -> AIMessage:
    """AIMessage asking for tools: (name, args, id) triples, as the LLM would emit."""
    tool_calls = [
        {"name": name, "args": args, "id": call_id} for name, args, call_id in calls
    ]
    return AIMessage(content="", tool_calls=tool_calls)


# --- should_continue branching ------------------------------------------


def test_should_continue_final_answer_goes_to_responder() -> None:
    state = make_state(messages=[AIMessage(content="The answer is 42.")])
    assert should_continue(state) == "responder"


def test_should_continue_tool_call_goes_to_tool_exec() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "1+1"}, "t1"))]
    )
    assert should_continue(state) == "tool_exec"


def test_should_continue_cap_overrides_tool_call() -> None:
    # Even though the model wants more tools, the budget guardrail wins.
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "1+1"}, "t1"))],
        tool_iterations=TOOL_ITERATION_CAP,
    )
    assert should_continue(state) == "responder"


def test_should_continue_empty_history_raises() -> None:
    with pytest.raises(ValueError, match="empty history"):
        should_continue(make_state())


# --- route_edge (pure, bonus) -------------------------------------------


def test_route_edge_maps_known_route() -> None:
    assert route_edge({"route": "knowledge"}) == "knowledge"


def test_route_edge_unknown_or_missing_route_goes_to_responder() -> None:
    assert route_edge({"route": "music"}) == "responder"
    assert route_edge({"route": None}) == "responder"
    assert route_edge({}) == "responder"


# --- tool_exec_node: dispatch -------------------------------------------


def test_tool_exec_dispatches_known_tool() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "2+3"}, "c1"))]
    )
    out = tool_exec_node(state)

    (msg,) = out["messages"]
    assert isinstance(msg, ToolMessage)
    assert msg.content == "5"
    assert msg.tool_call_id == "c1"
    assert out["tool_iterations"] == 1


def test_tool_exec_unknown_tool_is_safe() -> None:
    state = make_state(messages=[_tool_calls(("no_such_tool", {}, "c1"))])
    (msg,) = tool_exec_node(state)["messages"]
    assert msg.content == "Unknown tool: no_such_tool"


def test_tool_exec_multiple_calls_one_tool_message_each() -> None:
    state = make_state(
        messages=[
            _tool_calls(
                ("calculate", {"expression": "1+1"}, "c1"),
                ("calculate", {"expression": "2+2"}, "c2"),
            )
        ],
    )
    out = tool_exec_node(state)
    assert [m.content for m in out["messages"]] == ["2", "4"]
    assert out["tool_iterations"] == 2


# --- tool_exec_node: exception boundary ---------------------------------


@tool
def _boom(value: str) -> str:
    """A tool that always raises — for the exception-wrapping test."""
    raise RuntimeError("kaboom")


def test_tool_exec_wraps_tool_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    # Inject a raising tool into the registry the node actually reads.
    monkeypatch.setattr("app.graph.nodes.tool_exec.tools_by_name", {"boom": _boom})
    state = make_state(messages=[_tool_calls(("boom", {"value": "x"}, "c1"))])

    (msg,) = tool_exec_node(state)["messages"]
    assert isinstance(msg, ToolMessage)
    assert msg.content == "Tool error: RuntimeError: kaboom"


# --- tool_iterations cap ------------------------------------------------


def test_tool_iterations_cap_stops_the_loop() -> None:
    # Two calls starting one below the cap: both run, then the 'cap reached'
    # SystemMessage is appended so the responder knows to stop and explain.
    state = make_state(
        messages=[
            _tool_calls(
                ("calculate", {"expression": "1+1"}, "c1"),
                ("calculate", {"expression": "1+1"}, "c2"),
            )
        ],
        tool_iterations=TOOL_ITERATION_CAP - 1,
    )
    out = tool_exec_node(state)

    assert out["tool_iterations"] == TOOL_ITERATION_CAP + 1
    assert isinstance(out["messages"][-1], SystemMessage)
    assert "iteration cap" in str(out["messages"][-1].content)


def test_tool_iterations_below_cap_has_no_system_message() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "1+1"}, "c1"))],
        tool_iterations=1,
    )
    out = tool_exec_node(state)

    assert isinstance(out["messages"][-1], ToolMessage)  # no SystemMessage
    assert out["tool_iterations"] == 2
