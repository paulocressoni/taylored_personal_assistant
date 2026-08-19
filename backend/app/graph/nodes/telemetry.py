"""telemetry_node — write the per-run LLM-call count into the final state.

Counts come from the RunTelemetry callback handler attached at the invoke
site (see app.graph.cli). tool_iterations is already maintained by
tool_exec_node, so this node only fills the field nothing else touches.
"""

from langchain_core.runnables import RunnableConfig

from app.graph.state import IPAState


def telemetry_node(state: IPAState, config: RunnableConfig) -> dict:
    # The counter instance travels inside config["configurable"], set at the
    # invoke site (see app.graph.cli). Reading it with .get() keeps this node
    # harmless if it ever runs without the counter attached (e.g. a bare
    # graph.invoke in a test): we fall back to whatever llm_calls already
    # holds instead of raising KeyError mid-graph.
    telemetry = config.get("configurable", {}).get("run_telemetry")
    llm_calls = telemetry.llm_calls if telemetry is not None else state["llm_calls"]
    return {"llm_calls": llm_calls}
