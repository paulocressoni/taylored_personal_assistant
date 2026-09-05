"""Unit tests for the pure helpers in app.graph.utils (BE-07)."""

from langchain_core.messages import AIMessage, HumanMessage

from app.graph.utils import trim_messages

# --- trim_messages: no cap / within limit ----------------------------------


def test_trim_disabled_returns_same_list() -> None:
    messages = [HumanMessage(content="a"), HumanMessage(content="b")]
    assert trim_messages(messages, max_messages=0) is messages
    assert trim_messages(messages, max_messages=-1) is messages


def test_trim_within_limit_returns_same_list() -> None:
    messages = [
        HumanMessage(content="a"),
        HumanMessage(content="b"),
        HumanMessage(content="c"),
    ]
    assert trim_messages(messages, max_messages=5) is messages


# --- trim_messages: capping -------------------------------------------------


def test_trim_keeps_only_the_tail() -> None:
    messages = [HumanMessage(content=f"m{i}") for i in range(25)]
    trimmed = trim_messages(messages, max_messages=20)
    assert len(trimmed) == 20
    assert trimmed[0].content == "m5"  # oldest in-window message
    assert trimmed[-1].content == "m24"  # newest message preserved


def test_trim_window_starts_on_a_human_turn() -> None:
    # Tail window would open on an AIMessage; leading non-Human messages
    # are dropped so the model never starts mid-exchange.
    messages = [
        HumanMessage(content=f"h{i}") if i % 2 == 0 else AIMessage(content=f"a{i}")
        for i in range(25)
    ]
    trimmed = trim_messages(messages, max_messages=20)
    assert isinstance(trimmed[0], HumanMessage)
    assert trimmed[0].content == "h6"  # h5 was an AIMessage and got dropped
    assert trimmed[-1].content == "h24"


def test_trim_window_with_no_human_returns_whole_tail() -> None:
    messages = [AIMessage(content=f"a{i}") for i in range(30)]
    trimmed = trim_messages(messages, max_messages=20)
    assert len(trimmed) == 20  # never returns an empty list
    assert all(isinstance(m, AIMessage) for m in trimmed)
