"""responder_node with a faked model — asserts it caps the model window (BE-07)."""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.core.config import settings
from app.graph.nodes.responder import responder_node


def test_responder_caps_history_sent_to_model(patch_llm) -> None:
    cap = settings.max_history_messages
    history = [HumanMessage(content=f"m{i}") for i in range(cap + 5)]
    fake = FakeChatModel(AIMessage(content="hello"))
    patch_llm(lambda role: fake)

    state = make_state(messages=history, lang="en")
    result = responder_node(state, config=None)

    # The node returns the model's answer (which the graph persists).
    (out,) = result["messages"]
    assert isinstance(out, AIMessage)

    # The model only ever saw system prompt + the capped window.
    sent = fake.calls[0]
    assert isinstance(sent[0], SystemMessage)
    assert len(sent) - 1 == cap
    assert sent[1].content == "m5"  # oldest messages were dropped
    assert sent[-1].content == f"m{cap + 4}"  # newest message preserved

    # The persisted history was NOT trimmed or mutated.
    assert len(state["messages"]) == cap + 5
