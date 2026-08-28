"""FastAPI app for the containerized backend (M09).

The lifespan context manager replaces the old @app.on_event("startup"):
it compiles the LangGraph graph ONCE at boot and stores it on app.state.
Every request reads that same compiled instance via Depends(get_graph) -
or request.app.state.graph - we never recompile per-request.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.llm import ROLE_CONFIG, get_chat_model
from app.core.observability import flush
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
    # Compilation instantiates the langchain runnables and edges. Doing it
    # once here instead of per-request removes both latency and churn.
    app.state.graph = build_graph()

    # Pre-warm the @cache'd chat models (they're created lazily inside the
    # nodes). The FIRST request now skips the one-time model construction.
    for role in ROLE_CONFIG:
        get_chat_model(role)

    logger.info("graph compiled and chat models pre-warmed")
    yield
    # --- shutdown ---------------------------------------------------------
    # Best-effort: flush any queued Langfuse events before the process ends.
    flush()


app = FastAPI(
    title="Taylored Personal Assistant",
    version="0.9.0",
    lifespan=lifespan,
)
app.include_router(router)
