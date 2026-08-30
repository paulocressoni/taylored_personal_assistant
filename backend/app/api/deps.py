"""Request plumbing: the compiled graph + how to seed one run (M09).

get_graph is the star — a FastAPI *dependency*. Because it is a plain
function with a `request` parameter, tests can replace the real graph with a
fake via app.dependency_overrides[get_graph] = fake_graph. That is the whole
trick that lets us test the HTTP layer without ever hitting DeepSeek.
"""

from typing import Any

from fastapi import Request
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

from app.core.callbacks import RunTelemetry
from app.core.observability import langfuse_metadata, new_langfuse_handler
from app.graph.state import IPAState


def get_graph(request: Request) -> Any:
    """Return the graph compiled ONCE at startup (see main.py lifespan).

    We read it off request.app.state — NOT a module global — because
    FastAPI replaces `request` during tests, which is what makes the
    dependency overridable.

    Args:
        request: FastAPI Request object, which has a reference to the app.

    Returns:
        The compiled graph, which is a callable that takes an IPAState and
        returns an IPAState.
    """
    return request.app.state.graph


def build_initial_state(
    user_input: str,
    session_id: str,
    channel: str,
    device_id: str | None,
) -> IPAState:
    """Seed the same 12-field IPAState the CLI seeds, but for an HTTP turn.

    The user's text becomes a HumanMessage so the responder model sees a
    proper message list.

    Args:
        user_input: The user's text input.
        session_id: The ID of the user's session.
        channel: The channel through which the request was received.
        device_id: The ID of the device from which the request was received.

    Returns:
        An initialized IPAState dictionary.
    """
    return {
        "messages": [HumanMessage(content=user_input)],
        "user_input": user_input,
        "session_id": session_id,
        "channel": channel,
        "device_id": device_id,
        "principal": None,
        "lang": None,
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
        "tools_called": [],
    }


def build_run_config(session_id: str, channel: str) -> dict[str, Any]:
    """Thread per-run telemetry + Langfuse through the graph, like the CLI.

    One RunTelemetry per request (never reused across turns). Langfuse stays
    best-effort: new_langfuse_handler() returns None when it's disabled, and
    we simply don't attach it.

    Args:
        session_id: The ID of the user's session.
        channel: The channel through which the request was received.

    Returns:
        A dictionary containing the run configuration.
    """
    telemetry = RunTelemetry()
    callbacks: list[BaseCallbackHandler] = [telemetry]
    langfuse = new_langfuse_handler()
    if langfuse is not None:
        callbacks.append(langfuse)
    return {
        "callbacks": callbacks,
        "configurable": {"run_telemetry": telemetry},
        "metadata": langfuse_metadata(session_id=session_id, channel=channel),
    }
