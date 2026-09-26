"""telemetry_node — write per-run usage into state.

This node runs LAST in the graph (after `responder`). It is the single place
that reconciles the final state with the RunTelemetry callback handler
attached at the invoke site (see app.graph.cli / app.api.deps): the counter
travels inside `config["configurable"]["run_telemetry"]` and has seen every
model call in the run — including the router's classification call.

The Langfuse trace is enriched by the INVOKE SITE instead
(app.core.observability.turn_span / enrich_trace), because a trace's identity
can only be written while the app-owned root span is open — and by the time
this node runs, that span's children are the ones on the stack.

When the RunTelemetry callback is present, this node persists a full
`usage` dict (tokens, prompt-cache split, error buckets) into state so the
checkpointer carries it across turns and the invoke site can read it back to
enrich the trace.
"""

from langchain_core.runnables import RunnableConfig

from app.graph.state import IPAState


def telemetry_node(state: IPAState, config: RunnableConfig) -> dict:
    """Reconcile the run's telemetry into state.

    Args:
        state: The current graph state, including route, language and any
            tools already called this run.
        config: The run configuration. May carry the shared `RunTelemetry`
            instance under `configurable.run_telemetry`.

    Returns:
        A state update overwriting `llm_calls` with the authoritative per-run
        total from the callback (or the state's own counter when no callback
        was attached) and, when the callback exists, the aggregated `usage`
        dict for persistence.
    """
    # Reading with .get() keeps this node harmless if it ever runs without
    # the counter attached (e.g. a bare graph.invoke in a test): we fall
    # back to whatever llm_calls already holds instead of raising mid-graph.
    telemetry = config.get("configurable", {}).get("run_telemetry")
    if telemetry is None:
        return {"llm_calls": state["llm_calls"]}

    totals = telemetry.totals()
    usage = {
        "llm_calls": totals["llm_calls"],
        "input_tokens": totals["input_tokens"],
        "output_tokens": totals["output_tokens"],
        "cache_hit_tokens": totals["cache_hit_tokens"],
        "cache_miss_tokens": totals["cache_miss_tokens"],
        "cache_hit_ratio": totals["cache_hit_ratio"],
        "error_types": totals["error_types"],
    }
    return {"llm_calls": int(usage["llm_calls"]), "usage": usage}
