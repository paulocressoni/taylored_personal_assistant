"""Unit tests for the single tool-using assistant node.

The fake model records every request, so these tests prove the contracts that
matter: one call for a direct answer, the real tool registry is bound, the
model window is capped, the voice addendum is last, and an unusable empty
response recovers without tools.
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.graph.nodes.assistant import assistant_node
from app.tools.registry import TOOLS


async def test_assistant_node_answers_directly_with_one_call(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="Paris is the capital."))
    patch_llm(lambda role: fake)

    state = make_state(
        user_input="What is the capital of France?",
        messages=[HumanMessage(content="What is the capital of France?")],
    )
    result = await assistant_node(state, config=None)

    (out,) = result["messages"]
    assert isinstance(out, AIMessage)
    assert out.content == "Paris is the capital."
    assert len(fake.calls) == 1
    assert fake.bound_tools == TOOLS

    system = fake.calls[0][0]
    assert isinstance(system, SystemMessage)
    assert "You are the assistant of a self-hosted smart home." in system.content
    assert system.content.endswith("Reply in en.")


async def test_assistant_node_prompt_carries_policy_not_tool_schemas(
    patch_llm,
) -> None:
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    await assistant_node(make_state(), config=None)

    system = fake.calls[0][0]
    # Tool schemas travel through bind_tools; duplicating them in the prompt
    # would cost tokens and drift from the registry.
    assert "calculate" not in system.content
    assert "When you call a tool, emit no user-facing text." in system.content


async def test_assistant_node_returns_the_tool_call_message(patch_llm) -> None:
    fake = FakeChatModel(
        AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "0.15 * 240"}, "id": "1"}
            ],
        )
    )
    patch_llm(lambda role: fake)

    result = await assistant_node(make_state(), config=None)

    (out,) = result["messages"]
    assert isinstance(out, AIMessage)
    assert out.tool_calls[0]["name"] == "calculate"
    assert fake.bound_tools == TOOLS


async def test_assistant_node_caps_history_sent_to_model(patch_llm) -> None:
    cap = settings.max_history_messages
    history = [HumanMessage(content=f"m{i}") for i in range(cap + 5)]
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    state = make_state(messages=history)
    await assistant_node(state, config=None)

    sent = fake.calls[0]
    assert isinstance(sent[0], SystemMessage)
    assert len(sent) - 1 == cap
    assert sent[1].content == "m5"
    assert sent[-1].content == f"m{cap + 4}"
    assert state["messages"] == history


async def test_assistant_node_uses_the_voice_role_and_addendum(patch_llm) -> None:
    roles: list[str] = []
    fake = FakeChatModel(AIMessage(content="ok"))

    def factory(role: str) -> FakeChatModel:
        roles.append(role)
        return fake

    patch_llm(factory)

    await assistant_node(make_state(channel="voice"), config=None)

    assert roles == ["assistant_voice"]
    system = fake.calls[0][0]
    assert "SPOKEN ANSWER MODE" in system.content
    # The dynamic language line must be last, after every addendum.
    assert system.content.endswith("Reply in en.")


async def test_assistant_node_defaults_lang_when_missing(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    state = make_state()
    state.pop("lang", None)
    await assistant_node(state, config=None)

    assert fake.calls[0][0].content.endswith("Reply in en.")


async def test_assistant_node_recovers_from_an_empty_response(patch_llm) -> None:
    fake = FakeChatModel(
        AIMessage(content=""),
        AIMessage(content="Please rephrase that."),
    )
    patch_llm(lambda role: fake)

    result = await assistant_node(make_state(), config=None)

    (out,) = result["messages"]
    assert isinstance(out, AIMessage)
    assert out.content == "Please rephrase that."
    assert len(fake.calls) == 2
    assert "RECOVERY" in fake.calls[1][0].content
    assert fake.calls[1][0].content.endswith("Reply in en.")


async def test_assistant_node_keeps_a_non_empty_truncated_answer(patch_llm) -> None:
    fake = FakeChatModel(
        AIMessage(
            content="A partial but useful answer.",
            response_metadata={"finish_reason": "length"},
        )
    )
    patch_llm(lambda role: fake)

    result = await assistant_node(make_state(), config=None)

    (out,) = result["messages"]
    assert out.content == "A partial but useful answer."
    assert len(fake.calls) == 1
