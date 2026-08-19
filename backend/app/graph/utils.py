"""Small, pure helpers shared across graph nodes."""

from langchain_core.messages import AnyMessage


def get_last_message(messages: list[AnyMessage]) -> AnyMessage:
    """Return the most recent message in the conversation.

    Raises:
        ValueError: if ``messages`` is empty.
    """
    if not messages:
        raise ValueError("Cannot get last message of an empty history.")
    return messages[-1]
