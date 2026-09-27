"""Langfuse observability wiring — SDK v4.

Langfuse v4 is OpenTelemetry-based. The LangChain ``CallbackHandler`` is
constructed with NO args — it binds to the process-wide singleton client
obtained via ``get_client()``. Per-trace attributes are passed through the
LangChain invoke ``config["metadata"]`` using the reserved ``langfuse_*``
keys; the handler reads them at root-run start and propagates them to the
trace and all child spans via OpenTelemetry baggage.

The run's OWN identity (trace name, tags, metadata) is not known up front —
route, tools and usage are only decided mid-run. It is stamped at the END of
the run by ``enrich_trace``, which only works while an app-owned root span is
open: Langfuse reads a trace's identity from its app-root span, and
``propagate_attributes`` only affects the span that is current. Every invoke
site therefore wraps its graph call in ``turn_span()``.

Best-effort by design: if Langfuse isn't configured, every helper is a no-op
and the graph behaves exactly as it did before M08.
"""

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, NamedTuple

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler

from app._version import __version__
from app.core.config import settings

# Versions the SHAPE of the metadata we attach to traces — not the prompt
# text (langfuse reserves `prompt_version` for its Prompt Management).
# Bumped to 2.0 when tool_outcomes / permission_denials / usage / error
# taxonomies were added; raise it again whenever the metadata shape changes
# so old and new traces are distinguishable in dashboards.
# 2.1: the run's identity is written onto an app-owned root span as FLAT
# scalars (the SDK coerces values to str and drops anything over 200 chars).
TRACE_SCHEMA_VERSION = "2.1"

# Any single metadata VALUE is coerced to a string and silently DROPPED when
# longer than this, so only short scalars belong in trace metadata.
MAX_METADATA_VALUE_LEN = 200

# Fallback trace/span name used BEFORE the router has decided the route. The
# invoke site stamps it as `langfuse_trace_name` so a run that crashes, times
# out or is abandoned still appears named and filterable; `enrich_trace`
# refines it to `assistant:<route>` once the run has actually finished.
TURN_TRACE_NAME = "assistant:turn"

_client: Langfuse | None = None


def _get_client() -> Langfuse | None:
    """Lazily build the shared client from app settings. None when disabled."""
    global _client
    if not settings.langfuse_ready:
        return None
    if _client is None:
        # Constructing Langfuse(...) configures the singleton that get_client()
        # and the CallbackHandler will use. base_url is the v4 parameter name.
        _client = Langfuse(
            public_key=settings.langfuse_public_key.get_secret_value(),
            secret_key=settings.langfuse_secret_key.get_secret_value(),
            base_url=settings.langfuse_base_url,
            # Without an environment every span lands in "default", so
            # env-scoped dashboard filters and alert conditions match nothing.
            environment=settings.env,
            release=__version__,
        )
    return _client


@contextmanager
def turn_span(name: str = TURN_TRACE_NAME) -> Generator[None, None, None]:
    """Open the app-owned root observation for ONE graph run.

    Langfuse reads a trace's name, tags and metadata from its app-root span,
    and `propagate_attributes` only affects the span that is current — so the
    graph has to run inside a span WE own, and `enrich_trace` has to be called
    before that span closes. Without this the run's attributes are dropped.

    Args:
        name: Initial span name. The trace name itself is set later by
            `enrich_trace`, once the router has decided the route.

    Yields:
        None. No-op when observability is switched off.
    """
    client = _get_client()
    if client is None:
        yield
        return
    with client.start_as_current_observation(name=name, as_type="span"):
        yield


def new_langfuse_handler() -> CallbackHandler | None:
    """One handler per graph invocation. None when observability is off."""
    if _get_client() is None:
        return None
    # v4: no constructor args. All trace attributes come from invoke config.
    return CallbackHandler()


def langfuse_metadata(
    session_id: str,
    channel: str = "cli",
    user_id: str | None = None,
    **static: Any,
) -> dict[str, Any]:
    """Config['metadata'] dict the v4 handler understands.

    The reserved ``langfuse_*`` keys are consumed at root-run start; every
    other key is passed through as trace metadata automatically.

    The STATIC half of the trace identity is stamped here — the fallback trace
    name plus the `env:*` / `channel:*` tags — because it is known before the
    run starts. A run that crashes, times out or is abandoned then still shows
    up named and filterable; `enrich_trace` later overwrites the name with
    `assistant:<route>` and merges the `status:*` tag on success.

    Args:
        session_id: Conversation id, written as `langfuse_session_id` so the
            run's traces group into one Langfuse session.
        channel: Where the turn came from (`api` / `cli`); also stored as plain
            `channel` trace metadata for the dashboards.
        user_id: Optional user id, written as `langfuse_user_id`.
        **static: Extra keys stored as trace metadata.

    Returns:
        The metadata dict to pass in the invoke config.
    """
    metadata: dict[str, Any] = {
        "langfuse_session_id": session_id,
        "langfuse_trace_name": TURN_TRACE_NAME,
        "langfuse_tags": [f"env:{settings.env}", f"channel:{channel}"],
        "channel": channel,
        **static,
    }
    if user_id is not None:
        metadata["langfuse_user_id"] = user_id
    return metadata


def _error_taxonomy(tool_outcomes: list[dict[str, Any]]) -> dict[str, int]:
    """Bucket failed tool calls into the dashboard error taxonomy.

    Args:
        tool_outcomes: The run's `tool_outcomes` state records.

    Returns:
        error_type name -> count for each failed, non-denied call (denials are
        counted under their own metric, not as tool errors).
    """
    taxonomy: dict[str, int] = {}
    for outcome in tool_outcomes:
        if outcome.get("ok") or outcome.get("denied"):
            continue
        key = str(outcome.get("error_type") or "unknown")
        taxonomy[key] = taxonomy.get(key, 0) + 1
    return taxonomy


def _join_counts(counts: dict[str, Any] | None) -> str:
    """Render an error-bucket dict as the flat `name:count,...` string.

    Args:
        counts: Bucket name -> count, as produced by RunTelemetry/tool_exec.

    Returns:
        A comma-joined `name:count` string, empty when there is nothing to
        report. Flat and short on purpose: metadata values longer than
        `MAX_METADATA_VALUE_LEN` are dropped by the SDK.
    """
    if not counts:
        return ""
    return ",".join(f"{key}:{value}" for key, value in sorted(counts.items()))


def _tool_outcomes_summary(tool_outcomes: list[dict[str, Any]]) -> str:
    """Render each tool call as `name:outcome:duration_ms`, comma-joined.

    Args:
        tool_outcomes: The run's `tool_outcomes` state records.

    Returns:
        One entry per invocation (`calculate:ok:2.4`, `x:ValueError:0.0`,
        `x:denied:1.2`), truncated to `MAX_METADATA_VALUE_LEN` so the SDK
        never drops the whole value.
    """
    parts: list[str] = []
    for outcome in tool_outcomes:
        if outcome.get("denied"):
            state = "denied"
        elif outcome.get("ok"):
            state = "ok"
        else:
            state = str(outcome.get("error_type") or "error")
        parts.append(f"{outcome.get('tool')}:{state}:{outcome.get('duration_ms')}")
    return ",".join(parts)[:MAX_METADATA_VALUE_LEN]


class TraceAttributes(NamedTuple):
    """One finished run's trace identity, ready to be stamped on the trace."""

    name: str
    metadata: dict[str, str]
    tags: list[str]


def trace_attributes(state: dict[str, Any]) -> TraceAttributes:
    """Derive the trace name, metadata and tags for one finished run.

    Pure — no Langfuse call — so it is unit-testable without a running stack.
    Every metadata value is flattened to a SHORT string because the SDK
    coerces values with `str()` and drops any value over
    `MAX_METADATA_VALUE_LEN` characters; nested blobs (the whole `usage` dict,
    a multi-call `tool_outcomes` list) were discarded for exactly that reason.

    Args:
        state: The final graph state of the run.

    Returns:
        The trace name (`assistant:<route>`), the flat metadata dict and the
        low-cardinality tag list.
    """
    usage = state.get("usage") or {}
    tool_outcomes = state.get("tool_outcomes") or []
    tool_denials = sum(1 for outcome in tool_outcomes if outcome.get("denied"))
    tool_failures = sum(
        1
        for outcome in tool_outcomes
        if not outcome.get("ok") and not outcome.get("denied")
    )
    llm_error_types = usage.get("error_types") or {}

    status_tags: list[str] = []
    if llm_error_types:
        status_tags.append("status:llm_error")
    if tool_failures:
        status_tags.append("status:tool_error")
    if tool_denials or state.get("permission_denials"):
        status_tags.append("status:permission_denied")
    if not status_tags:
        status_tags.append("status:tool_ok" if tool_outcomes else "status:ok")

    route = state.get("route") or "unknown"
    metadata: dict[str, str] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "route": route,
        "lang": str(state.get("lang") or ""),
        "channel": str(state.get("channel") or ""),
        "tool_iterations": str(state.get("tool_iterations", 0)),
        "llm_calls": str(usage.get("llm_calls", state.get("llm_calls", 0))),
        "input_tokens": str(usage.get("input_tokens", 0)),
        "output_tokens": str(usage.get("output_tokens", 0)),
        "cache_hit_tokens": str(usage.get("cache_hit_tokens", 0)),
        "cache_miss_tokens": str(usage.get("cache_miss_tokens", 0)),
        "cache_hit_ratio": str(usage.get("cache_hit_ratio", 0.0)),
        "tools_called": ",".join(state.get("tools_called") or []),
        "tool_outcomes": _tool_outcomes_summary(tool_outcomes),
        "tool_error_taxonomy": _join_counts(_error_taxonomy(tool_outcomes)),
        "llm_error_taxonomy": _join_counts(llm_error_types),
    }
    tags = [
        f"env:{settings.env}",
        f"channel:{state.get('channel') or 'cli'}",
        *status_tags,
    ]
    return TraceAttributes(f"assistant:{route}", metadata, tags)


def enrich_trace(state: dict[str, Any]) -> None:
    """Stamp a finished run's name, tags and metadata onto the trace.

    MUST be called inside `turn_span()`: the app-root span is the only place
    Langfuse reads a trace's identity from, and the values have to be set
    before that span ends. No-op when observability is switched off.

    Args:
        state: The final graph state of the run.
    """
    if not settings.langfuse_ready:
        return
    attributes = trace_attributes(state)
    # The empty body is intentional: ENTERING the context is what writes the
    # attributes onto the current (app-root) span.
    with propagate_attributes(
        trace_name=attributes.name,
        metadata=attributes.metadata,
        tags=attributes.tags,
    ):
        pass


def timing_metadata(deltas_ms: dict[str, float]) -> dict[str, str]:
    """Render stage timings as flat string values for trace metadata.

    Pure, so the mapping is testable without Langfuse. Every trace metadata value
    is coerced to a string and silently DROPPED above `MAX_METADATA_VALUE_LEN`, so
    the numbers go out as short scalars rather than as one blob.

    Args:
        deltas_ms: Stage metric -> milliseconds, from `StageMarks.deltas_ms()`.

    Returns:
        The same keys mapped to one-decimal strings.
    """
    return {name: f"{value:.1f}" for name, value in deltas_ms.items()}


def stamp_voice_timing(deltas_ms: dict[str, float]) -> None:
    """Write one voice turn's stage timings onto the current observation.

    MUST be called while an app-owned span is open, for the same reason
    `enrich_trace` carries that constraint: `propagate_attributes` only affects
    the span that is current when the context is entered. No-op when
    observability is switched off.

    A dedicated timing span is deliberately NOT opened: the SDK fixes an
    observation's start time at creation, so a span created after the turn would
    report a duration near zero and sit in the wrong place on the timeline. The
    marks are attached to the span that was already open instead.

    Args:
        deltas_ms: Stage metric -> milliseconds, from `StageMarks.deltas_ms()`.
    """
    if not settings.langfuse_ready or not deltas_ms:
        return
    # Empty body is intentional: ENTERING the context is what writes the values.
    with propagate_attributes(metadata=timing_metadata(deltas_ms)):
        pass


def trace_url(handler: CallbackHandler) -> str | None:
    """Human URL for the last trace this handler created (v4 property)."""
    if handler.last_trace_id is None:
        return None
    return f"{settings.langfuse_base_url}/trace/{handler.last_trace_id}"


def flush() -> None:
    """Send queued events now. In v4, flush lives on the client, not the handler."""
    client = _get_client()
    if client is not None:
        client.flush()
