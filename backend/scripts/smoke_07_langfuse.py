"""smoke_07_langfuse.py — M08 proof: a CLI run becomes a Langfuse trace.

Requires the local Langfuse stack (make langfuse-up) and LANGFUSE_* keys in
.env.dev. Skips when observability is off so the fast suite stays green.

Run:    ENV=dev uv run python -m scripts.smoke_07_langfuse
Verify: open http://localhost:3000 -> Traces -> newest run -> expect a
        "assistant:knowledge" trace with spans for detect_lang / router /
        knowledge / responder and an LLM generation with token counts.
"""

import uuid
from typing import Any

import pytest
from langchain_core.messages import HumanMessage

from app.core.callbacks import RunTelemetry
from app.core.config import settings
from app.core.observability import (
    flush,
    langfuse_metadata,
    new_langfuse_handler,
    trace_url,
)
from app.graph.graph import build_graph
from app.graph.state import IPAState
from app.graph.utils import get_last_message

pytestmark = pytest.mark.integration


def _invoke(user_input: str) -> tuple[dict, Any]:
    """Run the graph with Langfuse wired up; return (final_state, handler).

    ``handler`` is None when observability is off — the smoke test just
    proceeds without trace assertions in that case.
    """
    session_id = uuid.uuid4().hex
    initial: IPAState = {
        "messages": [HumanMessage(content=user_input)],
        "user_input": user_input,
        "session_id": session_id,
        "channel": "cli",
        "device_id": None,
        "principal": None,
        "lang": None,
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
        "tools_called": [],
    }
    telemetry = RunTelemetry()
    langfuse = new_langfuse_handler()

    callbacks = [telemetry]
    configurable = {"run_telemetry": telemetry}
    metadata = langfuse_metadata(session_id=session_id, channel="cli")
    if langfuse is not None:
        callbacks.append(langfuse)

    graph = build_graph()
    result = graph.invoke(
        initial,
        config={
            "callbacks": callbacks,
            "configurable": configurable,
            "metadata": metadata,
        },
    )
    if langfuse is not None:
        flush()  # v4: client.flush(), so the trace lands before we print
    return result, langfuse


def test_smoke_07_langfuse_trace() -> None:
    if not settings.langfuse_ready:
        pytest.skip("Langfuse not configured (LANGFUSE_ENABLED/keys missing)")

    final, _langfuse = _invoke("What is the capital of France?")

    assert final["route"] == "knowledge"
    reply = str(get_last_message(final["messages"]).content)
    assert reply.strip()

    print(f"route={final['route']!r} lang={final['lang']!r}")
    print(f"reply={reply!r}")
    print("Now open http://localhost:3000 -> Traces -> look for the newest run.")


if __name__ == "__main__":
    final, langfuse = _invoke("What is the capital of France?")
    print("=== last message ===")
    print(get_last_message(final["messages"]).content)
    print(f"route={final['route']!r} lang={final['lang']!r}")
    # trace_url reads the handler's last_trace_id; _invoke already flushed.
    if langfuse is not None:
        print(f"=== langfuse trace: {trace_url(langfuse)} ===")
    print("Open http://localhost:3000 -> Traces to see the run.")
