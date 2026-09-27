"""responder_node unit tests — faked model, no network.

Complements test_responder.py (history-capping) with the node's other
contracts: the reply passthrough, the default-language fallback when ``lang``
was never resolved, the spoken-answer addendum on voice turns, and the promise
not to mutate the persisted history.
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, HumanMessage

from app.graph.nodes.responder import responder_node
from app.language.detector import DEFAULT_LANG


def test_responder_returns_the_model_reply(patch_llm) -> None:
    """The node surfaces exactly the AIMessage the model returned."""
    fake = FakeChatModel(AIMessage(content="the answer is 42"))
    patch_llm(lambda role: fake)

    result = responder_node(make_state(), config=None)

    (out,) = result["messages"]
    assert isinstance(out, AIMessage)
    assert out.content == "the answer is 42"


def test_responder_defaults_lang_when_unset(patch_llm) -> None:
    """A state with no ``lang`` falls back to DEFAULT_LANG in the prompt."""
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    # Simulate a turn where language was never resolved (key absent entirely,
    # not just None) — the node must not KeyError.
    state = make_state()
    state.pop("lang", None)
    responder_node(state, config=None)

    # The system message the model saw is the first entry of the recorded
    # invoke; the template substitutes DEFAULT_LANG into the "Reply in {lang}."
    # instruction.
    system = fake.calls[0][0]
    assert f"Reply in {DEFAULT_LANG}." in system.content


def test_responder_leaves_state_history_untouched(patch_llm) -> None:
    """trim_messages caps only what the MODEL sees; state is never mutated."""
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    history = [HumanMessage(content=f"m{i}") for i in range(5)]
    state = make_state(messages=history)
    responder_node(state, config=None)

    # Identical object list — nothing was trimmed or replaced in place.
    assert state["messages"] == history


def test_responder_switches_to_the_spoken_addendum_on_a_voice_turn(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    responder_node(make_state(channel="voice"), config=None)

    system = fake.calls[0][0]
    assert "SPOKEN ANSWER MODE" in system.content
    # The addendum must come AFTER the base prompt: adherence degrades with
    # distance from the response, and brevity is the rule we most need obeyed.
    assert system.content.index("SPOKEN ANSWER MODE") > system.content.index(
        "CAPABILITY HONESTY"
    )


def test_responder_keeps_the_base_rules_on_a_voice_turn(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    responder_node(make_state(channel="voice"), config=None)

    system = fake.calls[0][0]
    assert "Never claim to have performed an action" in system.content
    assert "Reply in en." in system.content


def test_responder_keeps_text_turns_out_of_spoken_mode(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="ok"))
    patch_llm(lambda role: fake)

    responder_node(make_state(channel="text"), config=None)

    assert "SPOKEN ANSWER MODE" not in fake.calls[0][0].content
