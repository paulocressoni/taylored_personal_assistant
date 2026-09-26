"""tool_exec_node — GENERIC tool dispatcher.

Looks each tool_call's name up in tools_by_name and invokes it. It knows
NOTHING about what the tools do — it only knows how to run a registry.
Every exception from a tool is captured and turned into a ToolMessage so a
tool failure can never crash the whole graph.

Each invocation also produces a structured OUTCOME dict (success / error type /
permission-denied / duration) that is appended to state["tool_outcomes"] via the
list reducer. The observability dashboards slice this per tool, so a trend of
failures or slow calls is visible without digging through logs.
"""

import logging
import time
from typing import Any

from langchain_core.messages import SystemMessage, ToolMessage

from app.graph.state import IPAState
from app.graph.utils import get_last_message
from app.tools.registry import tools_by_name

logger = logging.getLogger(__name__)

# Hard cap on ReAct loop iterations. Without this, a tool that keeps saying
# "I need more information" would spin forever and burn real API budget.
TOOL_ITERATION_CAP = 5

# Exception names a tool may raise to signal authorization refused. The
# identity/permission layer is not in the tree yet, so this is the
# convention future code (or a tool) opts into by raising these. Anything
# carrying a truthy `permission` attribute is ALSO treated as a denial, which
# lets a tool express "denied" without a bespoke exception type.
_DENIED_NAMES = {
    "PermissionError",
    "PermissionDenied",
    "Forbidden",
    "AuthError",
    "UnauthorizedError",
    "NotAuthorized",
}

# Tool error messages can be long; truncate before they land in trace metadata.
_MAX_ERROR_TEXT = 300


def _is_permission_denial(exc: BaseException) -> bool:
    """True when the exception signals an authorization refusal.

    Args:
        exc: The exception a tool raised.

    Returns:
        True for a builtin/raised PermissionError, a name in `_DENIED_NAMES`,
        or any exception carrying a truthy `permission` attribute.
    """
    name = type(exc).__name__
    return (
        isinstance(exc, PermissionError)
        or name in _DENIED_NAMES
        or bool(getattr(exc, "permission", False))
    )


def _outcome(
    tool: str, ok: bool, duration_ms: float, error_type: str | None, denied: bool
) -> dict[str, Any]:
    """Build one tool_outcomes record.

    Args:
        tool: Tool name as invoked.
        ok: Whether the invocation returned normally.
        duration_ms: Wall-clock time of the invocation, rounded to 0.1 ms.
        error_type: Exception class name when it failed, else None.
        denied: Whether the failure was an authorization refusal.

    Returns:
        A dict safe for trace metadata and the ClickHouse queries in
        `infra/observability/queries.sql`.
    """
    return {
        "tool": tool,
        "ok": ok,
        "duration_ms": round(duration_ms, 1),
        "error_type": error_type,
        "denied": denied,
    }


def tool_exec_node(state: IPAState) -> dict:
    """Run the tools requested by the last message.

    Args:
        state: The current graph state, including the last message's tool_calls.

    Returns:
        A dict with the tool results, updated iteration count, and structured
        outcomes for observability.
    """
    last = get_last_message(state["messages"])
    tool_calls = getattr(last, "tool_calls", None) or []

    iterations = state["tool_iterations"]
    results: list[ToolMessage] = []
    outcomes: list[dict[str, Any]] = []
    denials: list[dict[str, Any]] = []
    principal = state.get("principal") or {}
    for call in tool_calls:
        iterations += 1
        name = call["name"]
        args = call.get("args") or {}
        tool = tools_by_name.get(name)
        started = time.perf_counter()

        ok = True
        error_type: str | None = None
        denied = False
        content: str = ""
        if tool is None:
            ok = False
            error_type = "UnknownTool"
            content = f"Unknown tool: {name}"
        else:
            try:
                content = str(tool.invoke(args))
            except Exception as exc:  # noqa: BLE001 - tool boundary: never crash the graph
                logger.warning(
                    "tool %r failed with args %r: %s: %s",
                    name,
                    args,
                    type(exc).__name__,
                    exc,
                )
                ok = False
                error_type = type(exc).__name__
                denied = _is_permission_denial(exc)
                message = f"Tool error: {type(exc).__name__}: {exc}"
                content = message[:_MAX_ERROR_TEXT]
                if denied:
                    denials.append(
                        {
                            "tool": name,
                            "reason": type(exc).__name__,
                            "user_id": principal.get("user_id"),
                        }
                    )

        duration_ms = (time.perf_counter() - started) * 1000.0
        outcomes.append(_outcome(name, ok, duration_ms, error_type, denied))

        results.append(ToolMessage(content=str(content), tool_call_id=call["id"]))

    # Hand the tool names + structured outcomes to the reducers so
    # observability can report which tools ran and how each one fared.
    out: dict = {
        "messages": results,
        "tool_iterations": iterations,
        "tools_called": [call["name"] for call in tool_calls],
        "tool_outcomes": outcomes,
    }
    if denials:
        out["permission_denials"] = denials

    # Cap reached: stop the loop and tell the responder why, so it wraps up
    # with a clear message instead of the user seeing silence.
    if iterations >= TOOL_ITERATION_CAP:
        out["messages"] = results + [
            SystemMessage(
                content=(
                    "The tool loop reached its iteration cap and was stopped. "
                    "Do NOT call any more tools. Tell the user you couldn't "
                    "get a definitive answer and suggest rephrasing."
                )
            )
        ]
    return out
