"""Conversation checkpointing — where the graph's memory actually lives.

The checkpointer is created once at app startup (like the compiled graph)
and shared by every request. Keeping it in its own module means the
open/setup dance lives in exactly one place instead of cluttering main.py's
lifespan.

Two backends are supported, selected purely by configuration:

* Dev (default): a SQLite file via ``AsyncSqliteSaver``. Good enough for a
  single-process laptop run, zero infrastructure.
* Prod: Postgres via ``AsyncPostgresSaver`` (langgraph-checkpoint-postgres)
  whenever ``Settings.checkpoint_db_url`` is set. Postgres is what an
  always-on deployment needs — durable, concurrent, and backuppable with
  pg_dump (see the backup tooling).

``open_checkpointer()`` hides that difference: callers just
``async with open_checkpointer() as cp:`` and receive a ready saver whose
connection is open and whose tables already exist.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.core.config import settings


@asynccontextmanager
async def open_checkpointer() -> AsyncGenerator[BaseCheckpointSaver]:
    """Open a ready-to-use async checkpointer for the configured backend.

    Backend selection is driven entirely by ``Settings.checkpoint_db_url``:

    * unset/None -> SQLite file at ``Settings.checkpoint_db_path`` via
      ``AsyncSqliteSaver.from_conn_string``. In langgraph-checkpoint-sqlite
      3.x that call returns an async context manager and ``setup()`` is
      lazy, so no extra call is needed.
    * set -> Postgres via ``AsyncPostgresSaver.from_conn_string(dsn)`` (also
      an async context manager). Here ``setup()`` is NOT lazy — it creates
      the checkpoint tables — so we call it on every startup. It is
      idempotent (CREATE TABLE IF NOT EXISTS), which makes running it on
      every boot safe and doubles as the migration step for a fresh
      database.

    The ASYNC saver (never the sync one) is required because every HTTP
    endpoint is async — the graph runs through ``ainvoke`` /
    ``astream_events`` and LangGraph refuses async runs against a sync
    checkpointer.

    Yields:
        An initialized async saver with its checkpoint tables ready.
    """
    if settings.checkpoint_db_url:
        # Deferred import keeps psycopg (and its native libpq) out of
        # dev-only processes where the Postgres URL is never set.
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(
            settings.checkpoint_db_url
        ) as saver:
            # Create checkpoint tables if this is a fresh database. Runs on
            # every boot; idempotent, so a brand-new Postgres just works.
            await saver.setup()
            yield saver
        return

    async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db_path) as saver:
        yield saver
