"""Small, pure helpers shared across graph nodes and transports."""

from typing import Any

from langchain_core.messages import AnyMessage, HumanMessage


def get_last_message(messages: list[AnyMessage]) -> AnyMessage:
    """Return the most recent message in the conversation.

    Args:
        messages: The conversation message list (may be empty).

    Returns:
        The last message in the list.

    Raises:
        ValueError: if ``messages`` is empty.
    """
    if not messages:
        raise ValueError("Cannot get last message of an empty history.")
    return messages[-1]


def content_to_str(content: str | list[str | dict[Any, Any]]) -> str:
    """Flatten a message's content payload into plain text.

    ``AIMessage.content`` is a string or a list of text/dict blocks. The API,
    voice and assistant paths all need the same flattening, so this is the one
    implementation rather than a copy per transport.

    Args:
        content: A message's content payload.

    Returns:
        The concatenated text, or an empty string when there is none.
    """
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def trim_messages(
    messages: list[AnyMessage], max_messages: int | None
) -> list[AnyMessage]:
    """Return the history window a node should hand to the model.

    The checkpointer keeps the FULL conversation in ``state["messages"]`` (the
    session's memory), so without a cap long sessions would grow the list sent
    to every LLM call without bound. This helper builds the window for the
    MODEL only:

      1. keep the tail of ``messages`` up to ``max_messages`` long, then
      2. drop leading messages until the window starts at a HumanMessage.

    If the tail contains no HumanMessage (a long tool loop can fill it), the
    window extends back to the last HumanMessage in the full history so the
    model never receives an orphan ToolMessage whose AIMessage(tool_calls) was
    cut off. That means the result MAY be longer than ``max_messages``. When
    there is no HumanMessage anywhere, the result is empty: system prompt only
    is safer than an orphan tool result.

    Pure: never mutates the input. When the cap is disabled or not exceeded it
    returns the SAME list object (cheap no-op); when trimming is needed it
    returns a new sliced list.

    Args:
        messages: The full persisted message list (unbounded).
        max_messages: Preferred max messages to keep. Non-positive disables the
            cap.

    Returns:
        A window that starts at a HumanMessage, or ``messages`` unchanged when
        the cap is disabled or not exceeded.
    """
    if max_messages is None or max_messages <= 0:
        return messages
    if len(messages) <= max_messages:
        return messages

    # Trim the tail to the cap, then drop leading non-HumanMessage.
    window = messages[-max_messages:]
    for i, message in enumerate(window):
        if isinstance(message, HumanMessage):
            return window[i:]

    # The tail contains no user turn: walk back to the last HumanMessage in the
    # full history, keeping the tool exchange that follows it intact.
    for i in range(len(messages) - len(window) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i:]

    # No user turn anywhere: hand over no messages rather than orphan tools.
    return []
