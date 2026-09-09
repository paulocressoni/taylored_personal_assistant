"""telemetry_node — write per-run usage into state AND the Langfuse trace.

This node runs LAST in the graph (after `responder`). It is the single
place that reconciles the final state with the RunTelemetry callback handler
attached at the invoke site (see app.graph.cli / app.api.deps): the counter
travels inside `config["configurable"]["run_telemetry"]` and has seen every
model call in the run — including the router's classification call.

Because route / lang / tools_called / tool_outcomes are only decided mid-run,
this node is also the right place to stamp them onto the Langfuse trace via
enrich_trace before the root chain ends and flushes.

When the RunTelemetry callback is present, this node persists a full
`usage` dict (tokens, prompt-cache split, error buckets) into state so the
checkpointer carries it across turns and the API layer can surface it.
"""

from langchain_core.runnables import RunnableConfig

from app.core.observability import enrich_trace
from app.graph.state import IPAState


def telemetry_node(state: IPAState, config: RunnableConfig) -> dict:
    """Stamp the run's telemetry into state and enrich the Langfuse trace.

    Reads the RunTelemetry callback instance from the run config (set at the
    invoke site) so the count reflects EVERY model call in the run, not just
    the calls a node happened to persist to state. Enrichment is best-effort:
    `enrich_trace` is a no-op when Langfuse is disabled.

    Args:
        state: The current graph state, including route, language and any
            tools already called this run.
        config: The run configuration. May carry the shared `RunTelemetry`
            instance under `configurable.run_telemetry`.

    Returns:
        A state update overwriting `llm_calls` with the authoritative per-run
        total from the callback (or the state's own counter when no callback
        was attached) and, when the callback exists, the aggregated `usage`
        dict for persistence and trace enrichment.
    """
    # Reading with .get() keeps this node harmless if it ever runs without
    # the counter attached (e.g. a bare graph.invoke in a test): we fall
    # back to whatever llm_calls already holds instead of raising mid-graph.
    telemetry = config.get("configurable", {}).get("run_telemetry")
    if telemetry is None:
        llm_calls = state["llm_calls"]
        # Stamp route/lang/tools onto the trace before the run flushes. The
        # merged dict ensures enrich_trace sees the authoritative llm_calls.
        enrich_trace({**state, "llm_calls": llm_calls})
        return {"llm_calls": llm_calls}

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
    llm_calls = int(usage["llm_calls"])
    enrich_trace({**state, "llm_calls": llm_calls, "usage": usage})

    return {"llm_calls": llm_calls, "usage": usage}
