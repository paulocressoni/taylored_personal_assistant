"""Graph state schema — the single source of truth for what flows
between nodes. Extend here, never by ad-hoc dicts inside nodes."""

import operator
from typing import Annotated, NotRequired, TypedDict

from langchain_core.messages import AnyMessage


class IPAState(TypedDict):
    # operator.add on a list is a REDUCER: when a node returns a list,
    # LangGraph MERGES it with what's already there instead of overwriting.
    # This is how conversation history accumulates turn after turn.
    #
    # This list is the PERSISTED session memory and intentionally
    # stays unbounded — the checkpointer stores it and GET /sessions/{id}/
    # history reads it back whole. We never mutate it. Instead, the nodes
    # that call the LLM (responder, knowledge) cap what the MODEL sees via
    # Settings.max_history_messages and app.graph.utils.trim_messages when
    # they build their prompt input. Persistence unbounded, model window
    # bounded — two different concerns.
    messages: Annotated[list[AnyMessage], operator.add]
    user_input: str
    session_id: str
    channel: str  # "text" | "voice" | "cli"
    device_id: str | None  # which device this turn came from
    principal: dict | None  # {user_id, role, confidence, auth_method}
    lang: NotRequired[str | None]  # "en" | "de" | "pt-BR"; absent until
    # detect_lang_node fills it (BE-08) — ALWAYS read via state.get("lang")
    route: str | None
    pending_action: dict | None
    llm_calls: int
    tool_iterations: int
    tools_called: Annotated[list[str], operator.add]
