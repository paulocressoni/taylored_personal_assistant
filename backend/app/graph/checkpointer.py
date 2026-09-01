"""Conversation checkpointing — the persistent memory behind M11.

The checkpointer is created ONCE at app startup (like the compiled graph)
and shared by every request. Keeping it in its own module means the DB path
and the open/setup dance live in exactly one place instead of cluttering
main.py's lifespan.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

# SQLite file that survives across HTTP requests. A FILE (not ":memory:")
# also survives a `uvicorn --reload` restart — which is what makes the
# "refresh the browser tab" demo work even across a code reload.
# TODO: promote to app.core.config.Settings once paths become env-driven.
CHECKPOINT_DB_PATH = "checkpoints.db"


@asynccontextmanager
async def open_checkpointer() -> AsyncGenerator[AsyncSqliteSaver]:
    """Open (and create if needed) the SQLite-backed async checkpointer.

    This is itself an ASYNC CONTEXT MANAGER (not a plain function) because of
    how langgraph-checkpoint-sqlite 3.x is built: ``AsyncSqliteSaver.
    from_conn_string(...)`` returns an async context manager that opens the
    aiosqlite connection on entry and closes it on exit. There is no
    ``aclose()`` method to call, and ``setup()`` runs lazily on first use —
    so the caller simply does ``async with open_checkpointer() as cp:`` and
    the connection lives exactly as long as that block.

    We deliberately use the ASYNC saver (not the sync SqliteSaver) because
    every HTTP endpoint is async:
      - WS  /ws/chat  -> graph.astream_events(...)
      - POST /chat    -> graph.ainvoke(...)
    LangGraph refuses to run async with a sync checkpointer, so one async
    saver is the single shape that serves both routes.

    Yields:
        An initialized AsyncSqliteSaver with its checkpoints table ready.
    """
    async with AsyncSqliteSaver.from_conn_string(CHECKPOINT_DB_PATH) as saver:
        yield saver
