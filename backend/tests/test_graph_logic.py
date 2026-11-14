"""Unit tests for graph branching and the generic tool dispatcher.

``should_continue`` and ``after_tools`` are pure routing helpers, so they are
tested with plain state dicts. ``tool_exec_node`` never touches the LLM but is
async because tools are awaited; these tests are ``async def`` under
``asyncio_mode = "auto"``.
"""

import pytest
from conftest import make_state
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.graph.graph import after_tools, should_continue
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP, tool_exec_node


def _tool_calls(*calls: tuple[str, dict, str]) -> AIMessage:
    """Build an AIMessage requesting tools from (name, args, id) triples."""
    tool_calls = [
        {"name": name, "args": args, "id": call_id} for name, args, call_id in calls
    ]
    return AIMessage(content="", tool_calls=tool_calls)


def test_should_continue_final_answer_goes_to_telemetry() -> None:
    state = make_state(messages=[AIMessage(content="The answer is 42.")])
    assert should_continue(state) == "telemetry"


def test_should_continue_tool_call_goes_to_tool_exec() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "1+1"}, "t1"))]
    )
    assert should_continue(state) == "tool_exec"


def test_should_continue_empty_history_raises() -> None:
    with pytest.raises(ValueError, match="empty history"):
        should_continue(make_state())


def test_after_tools_below_cap_goes_to_assistant() -> None:
    assert after_tools(make_state(tool_iterations=1)) == "assistant"


def test_after_tools_at_cap_goes_to_finalize() -> None:
    assert after_tools(make_state(tool_iterations=TOOL_ITERATION_CAP)) == "finalize"


def test_after_tools_above_cap_goes_to_finalize() -> None:
    assert after_tools(make_state(tool_iterations=TOOL_ITERATION_CAP + 1)) == "finalize"


async def test_tool_exec_dispatches_known_tool() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "2+3"}, "c1"))]
    )
    out = await tool_exec_node(state)

    (msg,) = out["messages"]
    assert isinstance(msg, ToolMessage)
    assert msg.content == "5"
    assert msg.tool_call_id == "c1"
    assert out["tool_iterations"] == 1


async def test_tool_exec_unknown_tool_is_safe() -> None:
    state = make_state(messages=[_tool_calls(("no_such_tool", {}, "c1"))])
    (msg,) = (await tool_exec_node(state))["messages"]
    assert msg.content == "Unknown tool: no_such_tool"


async def test_tool_exec_multiple_calls_one_tool_message_each() -> None:
    state = make_state(
        messages=[
            _tool_calls(
                ("calculate", {"expression": "1+1"}, "c1"),
                ("calculate", {"expression": "2+2"}, "c2"),
            )
        ],
    )
    out = await tool_exec_node(state)
    assert [m.content for m in out["messages"]] == ["2", "4"]
    assert out["tool_iterations"] == 2


@tool
def _boom(value: str) -> str:
    """A tool that always raises, for the exception-wrapping test."""
    raise RuntimeError("kaboom")


async def test_tool_exec_wraps_tool_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.graph.nodes.tool_exec.tools_by_name", {"boom": _boom})
    state = make_state(messages=[_tool_calls(("boom", {"value": "x"}, "c1"))])

    (msg,) = (await tool_exec_node(state))["messages"]
    assert isinstance(msg, ToolMessage)
    assert msg.content == "Tool error: RuntimeError: kaboom"


async def test_tool_exec_at_cap_persists_no_system_message() -> None:
    state = make_state(
        messages=[
            _tool_calls(
                ("calculate", {"expression": "1+1"}, "c1"),
                ("calculate", {"expression": "1+1"}, "c2"),
            )
        ],
        tool_iterations=TOOL_ITERATION_CAP - 1,
    )
    out = await tool_exec_node(state)

    assert out["tool_iterations"] == TOOL_ITERATION_CAP + 1
    assert all(isinstance(message, ToolMessage) for message in out["messages"])
    assert len(out["messages"]) == 2


async def test_tool_exec_below_cap_has_only_tool_messages() -> None:
    state = make_state(
        messages=[_tool_calls(("calculate", {"expression": "1+1"}, "c1"))],
        tool_iterations=1,
    )
    out = await tool_exec_node(state)

    assert isinstance(out["messages"][-1], ToolMessage)
    assert out["tool_iterations"] == 2
