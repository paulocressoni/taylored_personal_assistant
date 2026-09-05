"""Small, pure helpers shared across graph nodes."""

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


def trim_messages(messages: list[AnyMessage], max_messages: int) -> list[AnyMessage]:
    """Return the history window a node should hand to the model.

    The checkpointer keeps the FULL conversation in state["messages"] (the
    session's memory), so without a cap long sessions would grow the list
    sent to every LLM call without bound. This helper builds the window
    for the MODEL only:

      1. keep the tail of ``messages`` up to ``max_messages`` long, then
      2. drop any leading messages that are not a HumanMessage so the
         window never opens mid-assistant / mid-tool-exchange.

    Pure: never mutates the input. When the cap is disabled or not
    exceeded it returns the SAME list object (cheap no-op); when trimming
    is needed it returns a new sliced list.

    Args:
        messages: The full persisted message list (unbounded).
        max_messages: Max messages to keep. Non-positive disables the cap.

    Returns:
        At most ``max_messages`` messages for the model, or ``messages``
        unchanged when the cap is disabled or not exceeded.
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
    # No user turn in the window: hand over the whole tail rather than [].
    return window
