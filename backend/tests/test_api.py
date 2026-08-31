"""HTTP layer tests (M09, checkpointing in M11): fakes, no network.

The routes read the compiled graph from app.state.graph (set by the
lifespan), so we swap that one attribute for the fake. Both /chat and
/ws/chat reach the same app instance, so a single assignment covers both.
M11 adds app.state.checkpointer — swapped for a FakeCheckpointer the same way.
"""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app.main import app


class FakeGraph:
    """Duck-typed stand-in for the compiled LangGraph.

    It only needs the two methods the routes call: ainvoke (for POST /chat,
    async since M11) and astream_events (for /ws/chat).
    """

    async def ainvoke(self, initial, config=None):
        return {
            "messages": [AIMessage(content="fake reply")],
            "lang": "en",
            "route": "responder",
        }

    async def astream_events(self, initial, config=None, version="v2"):
        # Mimic exactly the two events the WS handler looks for:
        #  1. a responder token chunk
        #  2. the root on_chain_end carrying the final state
        yield {
            "event": "on_chat_model_stream",
            "metadata": {"langgraph_node": "responder"},
            "data": {"chunk": AIMessageChunk(content="fake reply")},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {
                "output": {
                    "messages": [AIMessage(content="fake reply")],
                    "lang": "en",
                    "route": "responder",
                }
            },
        }


class FakeCheckpointTuple:
    """Minimal stand-in for a langgraph CheckpointTuple."""

    def __init__(self, channel_values: dict) -> None:
        self.checkpoint = {"channel_values": channel_values}


class FakeCheckpointer:
    """Duck-typed stand-in for AsyncSqliteSaver (only what routes call)."""

    def __init__(self) -> None:
        self._store: dict[str, FakeCheckpointTuple] = {}

    def seed(self, session_id: str, messages: list) -> None:
        """Pre-populate a thread so the history endpoint has data."""
        self._store[session_id] = FakeCheckpointTuple({"messages": messages})

    async def aget_tuple(self, config: dict) -> FakeCheckpointTuple | None:
        return self._store.get(config["configurable"]["thread_id"])


@pytest.fixture
def client():
    with TestClient(app) as c:  # context manager -> runs the lifespan
        app.state.graph = FakeGraph()  # swap the real graph for the fake
        app.state.checkpointer = FakeCheckpointer()  # ... and the DB for a fake
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_returns_reply_lang_route(client):
    r = client.post("/chat", json={"session_id": "s1", "message": "hello"})
    assert r.status_code == 200
    assert r.json() == {"reply": "fake reply", "lang": "en", "route": "responder"}


def test_chat_rejects_empty_message(client):
    r = client.post("/chat", json={"session_id": "s1", "message": ""})
    assert r.status_code == 422  # pydantic validation at the boundary


def test_ws_streams_tokens_then_done(client):
    with client.websocket_connect("/ws/chat") as ws:
        ws.send_json({"session_id": "s1", "message": "hi"})
        token = ws.receive_json()
        assert token == {"type": "token", "content": "fake reply"}
        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["reply"] == "fake reply"
        assert done["lang"] == "en"


# --- M11: session history ------------------------------------------------


def test_session_history_empty_for_unknown_session(client):
    r = client.get("/sessions/never-seen/history")
    assert r.status_code == 200
    assert r.json() == {"session_id": "never-seen", "messages": []}


def test_session_history_returns_persisted_messages(client):
    client.app.state.checkpointer.seed(
        "s1",
        [HumanMessage(content="hello"), AIMessage(content="hi there!")],
    )
    r = client.get("/sessions/s1/history")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s1"
    assert body["messages"] == [
        {"role": "human", "content": "hello"},
        {"role": "ai", "content": "hi there!"},
    ]
