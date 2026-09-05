"""detect_lang_node — resolve this turn's language into state["lang"].

Runs BEFORE the router so every downstream node (router, specialists,
responder) reads a resolved ``lang`` from state. Deterministic: no LLM call,
no tokens spent — just the lingua detector plus the M05 fallback chain.

Language continuity rides on PERSISTED STATE, not on re-detecting old turns.
``lang`` is a plain (non-reducer) channel, so after a turn the
checkpointer stores the RESOLVED language under state["lang"]. The per-turn
seeders (app.api.deps.build_initial_state, app.graph.cli) deliberately leave
``lang`` unset so that persisted value survives into the next run instead of
being overwritten with None. This node reads it back as the fallback
``previous_lang`` — which resolve_lang only consults when this turn's
detection confidence is below MIN_CONFIDENCE (rung 2 of the fallback chain).
"""

from langchain_core.runnables import RunnableConfig

from app.graph.state import IPAState
from app.language.detector import resolve_lang


def _previous_user_lang(state: IPAState) -> str | None:
    """Language of the most recent PRIOR turn, read from persisted state.

    state["lang"] already holds the language the assistant actually used on
    the previous turn: detect_lang_node resolves it every run and writes it
    back, and the checkpointer persists it between turns. We therefore do
    NOT walk the message list or re-run lingua on old text — continuity
    follows state, so trimming message history can never break it.

    Args:
        state (IPAState): The current IPA state.

    Returns:
        str | None: The resolved language of the most recent prior turn, or
            None when there is no previous turn (fresh thread / stateless
            run) — same behaviour as before persistence.
    """
    return state.get("lang")


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
