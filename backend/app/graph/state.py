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
    # detect_lang_node fills it — ALWAYS read via state.get("lang")
    # The language the speech-to-text request was BIASED with. An input to this
    # turn rather than its outcome, so it can differ from `lang`: a German
    # utterance transcribed while the bias was English. Required, not
    # NotRequired — LangGraph overwrites non-reducer channels from the input, so
    # a text turn seeding None CLEARS the previous voice turn's hint, whereas an
    # absent key would let that stale hint leak into the next trace.
    stt_lang: str | None
    route: str | None
    pending_action: dict | None
    llm_calls: int
    tool_iterations: int
    tools_called: Annotated[list[str], operator.add]
    # --- Observability additions ---
    # One dict per tool INVOCATION, appended by tool_exec_node through the
    # reducer so every ReAct iteration this turn is recorded, e.g.
    # {"tool": "calculate", "ok": false, "error_type": "ValueError",
    #  "denied": false, "duration_ms": 12.4}.
    tool_outcomes: Annotated[list[dict], operator.add]
    # Permission-denial records: {"tool": str, "reason": str,
    # "user_id": str | None}. Empty until authorization lands.
    permission_denials: Annotated[list[dict], operator.add]
    # Per-run LLM usage totals, written ONCE by telemetry_node at the end of a
    # run: {llm_calls, input_tokens, output_tokens, cache_hit_tokens,
    # cache_miss_tokens, cache_hit_ratio, error_types}. Read via .get().
    usage: dict | None
