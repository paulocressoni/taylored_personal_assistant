"""Request plumbing: the compiled graph + how to seed one run (M09, M11).

get_graph is the star — a FastAPI *dependency*. Because it is a plain
function with a `request` parameter, tests can replace the real graph with a
fake via app.dependency_overrides[get_graph] = fake_graph. That is the whole
trick that lets us test the HTTP layer without ever hitting DeepSeek.
"""

from typing import Any

from fastapi import Header, HTTPException, Request, WebSocket, status
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage

from app.api.ratelimit import SlidingWindowLimiter
from app.core.callbacks import RunTelemetry
from app.core.config import settings
from app.core.logging import set_log_context
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
        "tool_outcomes": [],
        "permission_denials": [],
        "usage": None,
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
    # Stamp the session/channel onto this request's log context BEFORE
    # the run starts, so every log line for this turn (route-level info, node
    # warnings, exceptions) carries the correlation id. FastAPI runs each
    # request handler in its own asyncio task (a private contextvars copy), so
    # this can never leak into the next request. Worker-thread logs inside
    # nodes are best-effort: they only see it if LangGraph's executor copies
    # the caller's context.
    set_log_context(session_id=session_id, channel=channel)
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


def build_rate_limiter() -> SlidingWindowLimiter:
    """Build the process-wide per-key rate limiter from Settings.

    Called ONCE in main.py's lifespan and stored on app.state — the same
    pattern as the compiled graph and checkpointer, so tests can swap it for
    a small-limit instance.

    Returns:
        A SlidingWindowLimiter configured from Settings.
    """
    return SlidingWindowLimiter(
        limit=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window_seconds,
    )


def require_rate_limit(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """FastAPI dependency: 429 when the API key exceeds its rate budget.

    Listed AFTER require_api_key in the route's dependencies, so only
    authenticated keys are counted (bad/missing keys 401 before this runs).
    The bucket identity is the presented key itself.

    Args:
        request: FastAPI Request carrying app.state.rate_limiter.
        x_api_key: The X-API-Key header value — the rate-limit identity.

    Raises:
        HTTPException: 429 Too Many Requests when the key is over budget.
    """
    # Read the limiter off request.app.state so tests can swap it for a
    # small-limit instance. The real limiter is built ONCE in main.py's
    # lifespan and stored on app.state.
    limiter: SlidingWindowLimiter = request.app.state.rate_limiter

    # If the key is over budget, raise a 429 with a Retry-After header.
    if not limiter.allow(x_api_key or "unknown"):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Please try again later.",
            headers={"Retry-After": str(int(settings.rate_limit_window_seconds))},
        )


def check_ws_rate_limit(websocket: WebSocket, key: str | None) -> bool:
    """True when the socket's presented key is still within its budget.

    Must be called AFTER the socket is accepted AND authorized (a close frame
    cannot be sent pre-accept). The key is passed in by the route because it
    may have arrived as a WebSocket subprotocol rather than in the
    query string — the route is the one that resolved it. Both transports
    therefore share one budget per key.

    Args:
        websocket: The accepted WebSocket to check.
        key: The presented API key (subprotocol token or ?api_key= value),
            as resolved by the route. None when the client sent no key.

    Returns:
        bool: True if allowed; False when the key exceeded its budget.
    """
    # Read the limiter off websocket.app.state so tests can swap it for a
    # small-limit instance. The real limiter is built ONCE in main.py's
    # lifespan and stored on app.state.
    limiter: SlidingWindowLimiter = websocket.app.state.rate_limiter
    return limiter.allow(key or "unknown")
