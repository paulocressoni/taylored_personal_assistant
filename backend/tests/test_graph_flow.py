"""End-to-end graph-flow tests through the faked model.

The fake is duck-typed and not a Runnable, so RunTelemetry never sees it; call
counts come from the fake itself. These tests cover the contract that matters:
one call for chat, two for a single tool round, the cap landing on the
tools-free finalize, and mode derived from tool_iterations.
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage

from app.api.deps import build_initial_state
from app.graph.graph import build_graph
from app.graph.nodes.telemetry import telemetry_node
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP
from app.graph.utils import get_last_message


def _turn(user_input: str) -> dict:
    return build_initial_state(
        user_input=user_input,
        session_id="flow-test",
        channel="text",
        device_id=None,
    )


async def test_chat_turn_uses_one_model_call_and_mode_chat(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="Paris."))
    patch_llm(lambda role: fake)

    final = await build_graph().ainvoke(
        _turn("Capital of France?"), config={"configurable": {}}
    )

    assert len(fake.calls) == 1
    assert final["mode"] == "chat"
    assert get_last_message(final["messages"]).content == "Paris."


async def test_one_tool_round_uses_two_model_calls_and_mode_tools(
    patch_llm,
) -> None:
    fake = FakeChatModel(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "1+1"}, "id": "c1"}
            ],
        ),
        AIMessage(content="2"),
    )
    patch_llm(lambda role: fake)

    final = await build_graph().ainvoke(
        _turn("What is 1+1?"), config={"configurable": {}}
    )

    assert len(fake.calls) == 2
    assert final["mode"] == "tools"
    assert final["tool_iterations"] == 1
    assert "calculate" in final["tools_called"]
    assert get_last_message(final["messages"]).content == "2"


async def test_cap_reaches_finalize_and_keeps_mode_tools(patch_llm) -> None:
    tool_calls = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "calculate",
                    "args": {"expression": "1+1"},
                    "id": f"c{index}",
                }
            ],
        )
        for index in range(TOOL_ITERATION_CAP)
    ]
    fake = FakeChatModel(*tool_calls, AIMessage(content="Here is what I have."))
    patch_llm(lambda role: fake)

    final = await build_graph().ainvoke(
        _turn("Keep trying."), config={"configurable": {}}
    )

    assert len(fake.calls) == TOOL_ITERATION_CAP + 1
    assert final["mode"] == "tools"
    assert get_last_message(final["messages"]).content == "Here is what I have."


class _StubTelemetry:
    """Minimal RunTelemetry surface for the usage/mode assertions."""

    def totals(self) -> dict:
        return {
            "llm_calls": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 10,
            "cache_hit_ratio": 0.0,
            "error_types": {},
        }


def test_telemetry_node_persists_usage_and_derives_mode() -> None:
    out = telemetry_node(
        make_state(tool_iterations=0),
        config={"configurable": {"run_telemetry": _StubTelemetry()}},
    )

    assert out["mode"] == "chat"
    assert out["usage"]["input_tokens"] == 10
    assert out["llm_calls"] == 1

    tools_out = telemetry_node(
        make_state(tool_iterations=1),
        config={"configurable": {"run_telemetry": _StubTelemetry()}},
    )
    assert tools_out["mode"] == "tools"
