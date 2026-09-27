"""FastAPI app for the containerized backend (M09, checkpointing in M11).

The lifespan context manager replaces the old @app.on_event("startup"):
it compiles the LangGraph graph ONCE at boot, opens the conversation
checkpointer, and stores both on app.state. Every request reads those same
instances via Depends(get_graph) / Depends(get_checkpointer) - or
request.app.state.graph - we never recompile (or reopen the DB) per-request.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app._version import __version__
from app.api.deps import build_rate_limiter
from app.api.routes import router
from app.core.config import settings
from app.core.llm import ROLE_CONFIG, get_chat_model
from app.core.logging import configure_logging
from app.core.observability import flush
from app.graph.checkpointer import open_checkpointer
from app.graph.graph import build_graph
from app.voice.registry import SessionSlots, build_voice_providers

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle for the whole app.

    Args:
        app: The FastAPI application instance.
    Yields:
        None. The function manages the startup and shutdown lifecycle of the app.
    """
    # --- startup ---------------------------------------------------------
    # Configure the ROOT logger BEFORE anything logs: dev -> readable
    # text, prod -> JSON. Runs every time the lifespan enters, so tests that
    # boot the app via TestClient get logging too.
    configure_logging()

    # open_checkpointer() is an ASYNC CONTEXT MANAGER: entering it opens the
    # SQLite connection and yields the saver; exiting it closes the connection.
    # We keep that context open for the WHOLE app lifetime, so the single
    # saver instance is alive for every request and is closed at shutdown.
    async with open_checkpointer() as checkpointer:
        # Store the checkpointer and graph on app.state so every request can read
        # them via Depends(get_graph) / Depends(get_checkpointer). We never
        # recompile the graph or reopen the checkpointer per-request.
        app.state.checkpointer = checkpointer

        # Build the LangGraph graph ONCE at startup and store it on app.state. The
        # graph is a callable that takes an IPAState and returns an IPAState.
        app.state.graph = build_graph(checkpointer=checkpointer)

        # Process-wide per-key rate limiter (in-memory, single worker).
        app.state.rate_limiter = build_rate_limiter()

        # Voice is optional. The provider clients own HTTP connection pools, so
        # they are built ONCE here and shared by every socket; the slot counter is
        # what stops a leaked connection from holding a provider quota for ever.
        app.state.voice_providers = build_voice_providers()
        app.state.voice_slots = SessionSlots(settings.voice_max_sessions)
        if app.state.voice_providers is None:
            logger.info("voice is off; voice sockets will be refused")

        # Pre-warm the @cache'd chat models (they're created lazily inside the
        # nodes). The FIRST request now skips the one-time model construction.
        for role in ROLE_CONFIG:
            get_chat_model(role)

        logger.info(
            "graph compiled, checkpointer open, chat models pre-warmed, voice=%s",
            "on" if app.state.voice_providers else "off",
        )

        yield
    # --- shutdown ---------------------------------------------------------
    # The `async with` above already closed the SQLite connection. Now flush
    # any queued Langfuse events best-effort before the process ends.
    flush()


app = FastAPI(
    title="Taylored Personal Assistant",
    version=__version__,
    lifespan=lifespan,
)

# Explicit CORS policy (never implicit). The stock dev UI is same-origin via
# the Vite proxy, so the default allowlist is empty = the browser blocks any
# cross-origin caller. Set CORS_ORIGINS in the environment to permit specific
# origins (a future non-proxied web client / dashboard). allow_credentials is
# off because we authenticate with a header (BE-01), not cookies; a wildcard
# origin must never be combined with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register the API router with the FastAPI application. All routes defined in
# the router will be available under the root path of the application.
app.include_router(router)
