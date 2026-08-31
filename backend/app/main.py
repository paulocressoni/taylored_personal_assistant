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

from app.api.routes import router
from app.core.llm import ROLE_CONFIG, get_chat_model
from app.core.observability import flush
from app.graph.checkpointer import open_checkpointer
from app.graph.graph import build_graph

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
    # open_checkpointer() is an ASYNC CONTEXT MANAGER: entering it opens the
    # SQLite connection and yields the saver; exiting it closes the connection.
    # We keep that context open for the WHOLE app lifetime, so the single
    # saver instance is alive for every request and is closed at shutdown.
    async with open_checkpointer() as checkpointer:
        app.state.checkpointer = checkpointer
        app.state.graph = build_graph(checkpointer=checkpointer)

        # Pre-warm the @cache'd chat models (they're created lazily inside the
        # nodes). The FIRST request now skips the one-time model construction.
        for role in ROLE_CONFIG:
            get_chat_model(role)

        logger.info("graph compiled, checkpointer open, chat models pre-warmed")
        yield
    # --- shutdown ---------------------------------------------------------
    # The `async with` above already closed the SQLite connection. Now flush
    # any queued Langfuse events best-effort before the process ends.
    flush()


app = FastAPI(
    title="Taylored Personal Assistant",
    version="0.9.0",
    lifespan=lifespan,
)
app.include_router(router)
