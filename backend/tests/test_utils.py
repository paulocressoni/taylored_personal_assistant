"""Unit tests for the pure helpers in app.graph.utils (BE-07).

``content_to_str`` is the one flattening seam shared by the API, voice and
assistant paths. ``trim_messages`` must never open a model window on an orphan
ToolMessage, even when a long tool loop has filled the tail.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.graph.utils import content_to_str, trim_messages

# --- content_to_str ---------------------------------------------------------


def test_content_to_str_returns_plain_strings_unchanged() -> None:
    assert content_to_str("hello") == "hello"


def test_content_to_str_joins_text_blocks() -> None:
    assert content_to_str(["hello", " ", "world"]) == "hello world"


def test_content_to_str_extracts_text_dict_blocks() -> None:
    assert (
        content_to_str([{"type": "text", "text": "hello"}, " world"]) == "hello world"
    )


def test_content_to_str_returns_empty_for_empty_content() -> None:
    assert content_to_str("") == ""
    assert content_to_str([]) == ""


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


def test_trim_window_extends_back_when_the_tail_is_a_tool_exchange() -> None:
    messages = [
        HumanMessage(content="q1"),
        AIMessage(content="a1"),
        HumanMessage(content="q2"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "calculate", "args": {"expression": "1+1"}, "id": "c1"}
            ],
        ),
        ToolMessage(content="2", tool_call_id="c1"),
    ]

    trimmed = trim_messages(messages, max_messages=2)

    # The tail [AIMessage(tool_calls), ToolMessage] has no HumanMessage, so the
    # window extends back to q2. It intentionally exceeds max_messages.
    assert trimmed[0] is messages[2]
    assert len(trimmed) == 3
    assert not isinstance(trimmed[0], ToolMessage)


def test_trim_window_with_no_human_returns_empty() -> None:
    messages = [AIMessage(content=f"a{i}") for i in range(30)]
    assert trim_messages(messages, max_messages=20) == []
