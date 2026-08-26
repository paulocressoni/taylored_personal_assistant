"""telemetry_node — write per-run usage into state AND the Langfuse trace.

Counts come from the RunTelemetry callback handler attached at the invoke
site (see app.graph.cli). This node runs LAST, so it's also the right place
to push route/lang/tool metadata onto the Langfuse trace via update_trace
before the root chain ends and flushes.

# TODO(api-layer): grow this node into the run-to-state bridge for usage
# telemetry once FastAPI + checkpointing land:
#   1. Extend RunTelemetry with a totals() method returning
#      {"llm_calls", "input_tokens", "output_tokens", "reasoning_tokens"}.
#   2. Add `usage: dict | None` to IPAState.
#   3. Return {"usage": telemetry.totals()} from this node so the API layer
#      can include cost in the response and the checkpointer can persist it
#      across turns for a session-level budget guard.
# Trigger: when backend/app/api gains a real graph.invoke() endpoint.
"""

# from langchain_core.runnables import RunnableConfig

# from app.core.observability import enrich_trace
# from app.graph.state import IPAState


# def telemetry_node(state: IPAState, config: RunnableConfig) -> dict:
#     # The counter instance travels inside config["configurable"], set at the
#     # invoke site (see app.graph.cli). Reading it with .get() keeps this node
#     # harmless if it ever runs without the counter attached (e.g. a bare
#     # graph.invoke in a test): we fall back to whatever llm_calls already
#     # holds instead of raising KeyError mid-graph.
#     telemetry = config.get("configurable", {}).get("run_telemetry")
#     llm_calls = telemetry.llm_calls if telemetry is not None else state["llm_calls"]

#     # Stamp route/lang/tools onto the trace before it flushes.
#     enrich_trace({**state, "llm_calls": llm_calls})

#     return {"llm_calls": llm_calls}
