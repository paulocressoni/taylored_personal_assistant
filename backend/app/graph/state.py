"""Graph state schema — the single source of truth for what flows
between nodes. Extend here, never by ad-hoc dicts inside nodes."""

import operator
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage


class IPAState(TypedDict):
    # operator.add on a list is a REDUCER: when a node returns a list,
    # LangGraph MERGES it with what's already there instead of overwriting.
    # This is how conversation history accumulates turn after turn.
    messages: Annotated[list[AnyMessage], operator.add]

    user_input: str
    session_id: str
    channel: str  # "text" | "voice" | "cli"
    device_id: str | None  # which device this turn came from
    principal: dict | None  # {user_id, role, confidence, auth_method}
    lang: str | None  # "en" | "de" | "pt-BR"
    route: str | None
    pending_action: dict | None
    llm_calls: int
    tool_iterations: int
    tools_called: Annotated[list[str], operator.add]
