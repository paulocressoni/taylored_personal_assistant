"""Langfuse observability wiring — SDK v4.

Langfuse v4 is OpenTelemetry-based. The LangChain ``CallbackHandler`` is
constructed with NO args — it binds to the process-wide singleton client
obtained via ``get_client()``. Per-trace attributes are passed through the
LangChain invoke ``config["metadata"]`` using the reserved ``langfuse_*``
keys; the handler reads them at root-run start and propagates them to the
trace and all child spans via OpenTelemetry baggage.

Best-effort by design: if Langfuse isn't configured, every helper is a no-op
and the graph behaves exactly as it did before M08.
"""

from typing import Any

from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler

from app.core.config import settings

# Versions the SHAPE of the metadata we attach to traces — not the prompt
# text (langfuse reserves `prompt_version` for its Prompt Management).
TRACE_SCHEMA_VERSION = "1.0"

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
        )
    return _client


def new_langfuse_handler() -> CallbackHandler | None:
    """One handler per graph invocation. None when observability is off."""
    if _get_client() is None:
        return None
    # v4: no constructor args. All trace attributes come from invoke config.
    return CallbackHandler()


def langfuse_metadata(
    session_id: str, user_id: str | None = None, **static: Any
) -> dict[str, Any]:
    """Config['metadata'] dict the v4 handler understands.

    The reserved ``langfuse_*`` keys are consumed at root-run start; every
    other key is passed through as trace metadata automatically.
    """
    metadata: dict[str, Any] = {
        "langfuse_session_id": session_id,
        **static,
    }
    if user_id is not None:
        metadata["langfuse_user_id"] = user_id
    return metadata


def enrich_trace(state: dict[str, Any]) -> None:
    """Update trace metadata mid-run. Call from the FINAL node.

    route/lang/tools_called are only decided mid-run, so they can't be in the
    invoke-time metadata. propagate_attributes writes onto the OpenTelemetry
    baggage of the current trace; the handler's root propagation context is
    active for the whole run, so these reach the trace before it flushes.
    """
    if not settings.langfuse_ready:
        return
    with propagate_attributes(
        trace_name=f"assistant:{state.get('route') or 'unknown'}",
        metadata={
            "route": state.get("route"),
            "lang": state.get("lang"),
            "tools_called": state.get("tools_called") or [],
            "schema_version": TRACE_SCHEMA_VERSION,
            "llm_calls": state.get("llm_calls", 0),
            "tool_iterations": state.get("tool_iterations", 0),
        },
        tags=[f"env:{settings.env}", f"channel:{state.get('channel') or 'cli'}"],
    ):
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
