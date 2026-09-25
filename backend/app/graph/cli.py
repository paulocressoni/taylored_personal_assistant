"""CLI entrypoint for the M03 hello graph.

Usage:
    ENV=dev uv run python -m app.graph.cli "capital of France?"
    make cli MSG='capital of France?'
"""

import datetime
import sys
import uuid

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

from app.core.callbacks import RunTelemetry
from app.core.observability import (
    enrich_trace,
    flush,
    langfuse_metadata,
    new_langfuse_handler,
    trace_url,
    turn_span,
)
from app.graph.graph import build_graph
from app.graph.state import IPAState
from app.graph.utils import get_last_message


def main() -> None:
    if len(sys.argv) < 2:
        print("usage: python -m app.graph.cli '<your message>'", file=sys.stderr)
        raise SystemExit(1)

    user_input = " ".join(sys.argv[1:])
    session_id = uuid.uuid4().hex

    # The initial state seeds every field EXCEPT ``lang``: on a
    # continuation turn the checkpointer's persisted lang must survive;
    # a stateless CLI run simply has none until detect_lang_node fills it.
    # Kept in lockstep with build_initial_state in app/api/deps.py. The
    # user's text becomes a HumanMessage so the responder model sees a
    # proper message list.
    initial: IPAState = {
        "messages": [HumanMessage(content=user_input)],
        "user_input": user_input,
        "session_id": session_id,
        "channel": "cli",
        "device_id": None,
        "principal": None,
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
        "tools_called": [],  # One seeded so the reducer has a base
        "tool_outcomes": [],  # Reducer base for the M28 per-tool outcome log
        "permission_denials": [],  # Reducer base for the M28 denial log
        "usage": None,
    }

    # One telemetry instance per run. "callbacks" makes the events fire for
    # every nested model/tool call; "configurable" exposes the SAME instance
    # to nodes so telemetry_node can write llm_calls into the final state.
    # The caller reads it directly for the summary line below.
    telemetry = RunTelemetry()
    langfuse = new_langfuse_handler()

    callbacks: list[BaseCallbackHandler] = [telemetry]
    configurable = {"run_telemetry": telemetry}
    metadata = langfuse_metadata(session_id=session_id, channel="cli")
    if langfuse is not None:
        callbacks.append(langfuse)

    graph = build_graph()
    # turn_span() opens the app-root span the trace's identity is written to.
    with turn_span():
        final = graph.invoke(
            initial,
            config={
                "callbacks": callbacks,
                "configurable": configurable,
                "metadata": metadata,  # <- v4 reads langfuse_* from here
                "checked_at": datetime.now(datetime.utc).isoformat(),
            },
        )
        enrich_trace(final)

    print(final)
    print("=== last message ===")
    print(get_last_message(final["messages"]).content)
    print(
        f"=== telemetry: {telemetry.llm_calls} LLM calls, "
        f"{final['tool_iterations']} tool iterations, "
        f"{telemetry.input_tokens}/{telemetry.output_tokens} tokens ==="
    )
    if langfuse is not None:
        flush()  # v4: client.flush(), not handler
        print(f"=== langfuse trace: {trace_url(langfuse)} ===")


if __name__ == "__main__":
    main()
