"""detect_lang_node — resolve this turn's language into state["lang"].

Runs BEFORE the router so every downstream node (router, specialists,
responder) reads a resolved ``lang`` from state. Deterministic: no LLM call,
no tokens spent — just the lingua detector plus the M05 fallback chain.
"""

from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig

from app.graph.state import IPAState
from app.language.detector import detect_lang, resolve_lang


def _previous_user_lang(state: IPAState) -> str | None:
    """Language of the most recent PRIOR user turn, if one exists.

    The current turn is state["user_input"] (the last HumanMessage's
    content), so we walk backwards to the first HumanMessage that isn't it.

    TODO(persistence): once real conversation checkpointing lands, read the
    persisted previous state["lang"] here instead of re-detecting.

    Args:
        state (IPAState): The current IPA state.

    Returns:
        str | None: The language tag of the most recent prior user turn, or None if not found.
    """
    # Walk backwards through the messages to find the most recent prior user turn
    current = state.get("user_input", "")
    for message in reversed(state.get("messages", [])):
        if isinstance(message, HumanMessage) and str(message.content) != current:
            detected, _confidence = detect_lang(str(message.content))
            return detected
    return None


def detect_lang_node(state: IPAState, config: RunnableConfig) -> dict:
    """Resolve and store this turn's language in state["lang"]. Never raises.

    Args:
        state (IPAState): The current IPA state.
        config (RunnableConfig): The runnable configuration.

    Returns:
        dict: A dictionary containing the resolved language tag.
    """
    lang = resolve_lang(
        state["user_input"],
        previous_lang=_previous_user_lang(state),
        preference=None,  # M13: per-user stored language preference
    )
    return {"lang": lang}
