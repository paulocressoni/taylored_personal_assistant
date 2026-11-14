"""Unit tests for the tools-free finalize node.

Finalize is the only node that never binds tools. It runs inside the same
history window as the assistant and carries the cap notice in its prompt
rather than in persisted state.
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.nodes.finalize import finalize_node
from app.graph.nodes.tool_exec import TOOL_ITERATION_CAP


async def test_finalize_node_binds_no_tools_and_uses_the_cap_addendum(
    patch_llm,
) -> None:
    fake = FakeChatModel(AIMessage(content="Here is what I have."))
    patch_llm(lambda role: fake)

    state = make_state(
        messages=[
            HumanMessage(content="What is 1+1?"),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "calculate", "args": {"expression": "1+1"}, "id": "c1"}
                ],
            ),
            ToolMessage(content="2", tool_call_id="c1"),
        ],
        tool_iterations=TOOL_ITERATION_CAP,
    )
    result = await finalize_node(state, config=None)

    (out,) = result["messages"]
    assert isinstance(out, AIMessage)
    assert out.content == "Here is what I have."
    assert fake.bound_tools is None
    assert len(fake.calls) == 1

    system = fake.calls[0][0]
    assert "TOOL LIMIT REACHED" in system.content
    assert system.content.endswith("Reply in en.")


async def test_finalize_node_uses_the_voice_role_and_addendum(patch_llm) -> None:
    roles: list[str] = []
    fake = FakeChatModel(AIMessage(content="ok"))

    def factory(role: str) -> FakeChatModel:
        roles.append(role)
        return fake

    patch_llm(factory)

    await finalize_node(
        make_state(channel="voice", tool_iterations=TOOL_ITERATION_CAP),
        config=None,
    )

    assert roles == ["assistant_voice"]
    system = fake.calls[0][0]
    assert "SPOKEN ANSWER MODE" in system.content
    assert "TOOL LIMIT REACHED" in system.content
    assert system.content.endswith("Reply in en.")
