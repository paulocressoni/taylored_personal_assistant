"""Request plumbing: the compiled graph + how to seed one run (M09, M11).

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


def get_checkpointer(request: Request) -> Any:
    """Return the process-wide AsyncSqliteSaver created at startup (M11).

    Same trick as get_graph: we read it off request.app.state — NOT a module
    global — so tests can swap it for a fake via app.state.checkpointer.

    Args:
        request: FastAPI Request object, which has a reference to the app.

    Returns:
        The AsyncSqliteSaver instance (or a test fake).
    """
    return request.app.state.checkpointer


def build_initial_state(
    user_input: str,
    session_id: str,
    channel: str,
    device_id: str | None,
) -> IPAState:
    """Seed the per-turn IPAState the CLI seeds, minus ``lang``.

    Every plain field is reset for this turn EXCEPT ``lang``: on a
    checkpointed continuation turn, LangGraph OVERWRITES non-reducer channels
    from the input, so seeding ``lang=None`` here would clobber the language
    the previous turn resolved to and break continuity. Leaving it unset lets
    the persisted value survive; on a brand-new thread there is nothing to
    preserve and detect_lang_node (the graph's first node) writes ``lang``
    before any reader runs. The user's text becomes a HumanMessage so the
    responder model sees a proper message list.

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
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
        "tools_called": [],
    }


def build_run_config(session_id: str, channel: str) -> dict[str, Any]:
    """Thread per-run telemetry + Langfuse through the graph, like the CLI.

    M11: ``configurable.thread_id`` is the KEY that ties every turn of one
    conversation together. It lives in ``config`` (NOT in IPAState), so
    LangGraph looks up the checkpoint for that thread before the run, merges
    the new turn into the saved history via the reducers, and writes the new
    state back afterwards. Same session_id -> same thread -> same memory.

    One RunTelemetry per request (never reused across turns). Langfuse stays
    best-effort: new_langfuse_handler() returns None when it's disabled, and
    we simply don't attach it.

    Args:
        session_id: The ID of the user's session (becomes the thread_id).
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
        "configurable": {
            "thread_id": session_id,
            "run_telemetry": telemetry,
        },
        "metadata": langfuse_metadata(session_id=session_id, channel=channel),
    }
