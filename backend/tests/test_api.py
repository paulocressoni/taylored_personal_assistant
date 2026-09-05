"""HTTP layer tests (M09, checkpointing in M11): fakes, no network.

The routes read the compiled graph from app.state.graph (set by the
lifespan), so we swap that one attribute for the fake. Both /chat and
/ws/chat reach the same app instance, so a single assignment covers both.
M11 adds app.state.checkpointer — swapped for a FakeCheckpointer the same way.
"""

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app._version import __version__
from app.core.config import settings
from app.main import app

# Every request must present the shared API key. It's required
# fail-fast, so the value always exists once ENV is set.
API_KEY = settings.assistant_api_key.get_secret_value()


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

    async def adelete_thread(self, thread_id: str) -> None:
        """Remove a thread's checkpoint; a no-op if the id never existed."""
        self._store.pop(thread_id, None)


@pytest.fixture
def client():
    with TestClient(app) as c:  # context manager -> runs the lifespan
        app.state.graph = FakeGraph()  # swap the real graph for the fake
        app.state.checkpointer = FakeCheckpointer()  # ... and the DB for a fake
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["version"] == __version__


def test_chat_returns_reply_lang_route(client):
    r = client.post(
        "/chat",
        json={"session_id": "s1", "message": "hello"},
        headers={"X-API-Key": API_KEY},
    )
    assert r.status_code == 200
    assert r.json() == {"reply": "fake reply", "lang": "en", "route": "responder"}


def test_chat_rejects_empty_message(client):
    r = client.post(
        "/chat",
        json={"session_id": "s1", "message": ""},
        headers={"X-API-Key": API_KEY},
    )
    assert r.status_code == 422  # pydantic validation at the boundary


def test_ws_streams_tokens_then_done(client):
    with client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as ws:
        ws.send_json({"session_id": "s1", "message": "hi"})
        token = ws.receive_json()
        assert token == {"type": "token", "content": "fake reply"}
        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["reply"] == "fake reply"
        assert done["lang"] == "en"


# --- M11: session history ------------------------------------------------


def test_session_history_empty_for_unknown_session(client):
    r = client.get("/sessions/never-seen/history", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    assert r.json() == {"session_id": "never-seen", "messages": []}


def test_session_history_returns_persisted_messages(client):
    client.app.state.checkpointer.seed(
        "s1",
        [HumanMessage(content="hello"), AIMessage(content="hi there!")],
    )
    r = client.get("/sessions/s1/history", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "s1"
    assert body["messages"] == [
        {"role": "human", "content": "hello"},
        {"role": "ai", "content": "hi there!"},
    ]


def test_delete_session_removes_persisted_session(client):
    client.app.state.checkpointer.seed(
        "s1",
        [HumanMessage(content="hello"), AIMessage(content="hi there!")],
    )
    r = client.delete("/sessions/s1", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    assert r.json() == {"session_id": "s1", "deleted": True}
    # The conversation is truly gone afterwards.
    h = client.get("/sessions/s1/history", headers={"X-API-Key": API_KEY})
    assert h.status_code == 200
    assert h.json()["messages"] == []


def test_delete_session_unknown_is_harmless_noop(client):
    r = client.delete("/sessions/never-seen", headers={"X-API-Key": API_KEY})
    assert r.status_code == 200
    assert r.json() == {"session_id": "never-seen", "deleted": True}


def test_delete_session_requires_api_key(client):
    r = client.delete("/sessions/s1")
    assert r.status_code == 401


# --- auth ---------------------------------------------------------


def test_chat_requires_api_key(client):
    r = client.post("/chat", json={"session_id": "s1", "message": "hello"})
    assert r.status_code == 401


def test_history_requires_api_key(client):
    r = client.get("/sessions/s1/history")
    assert r.status_code == 401


def test_ws_requires_api_key(client):
    from fastapi import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/chat") as ws,
    ):
        ws.receive_json()
    assert exc_info.value.code == 1008


# --- per-key rate limiting -----------------------------------------


def test_chat_rate_limited_returns_429(client):
    from app.api.ratelimit import SlidingWindowLimiter

    client.app.state.rate_limiter = SlidingWindowLimiter(limit=2, window_seconds=60)
    payload = {"session_id": "s1", "message": "hello"}
    for _ in range(2):
        r = client.post("/chat", json=payload, headers={"X-API-Key": API_KEY})
        assert r.status_code == 200
    r = client.post("/chat", json=payload, headers={"X-API-Key": API_KEY})
    assert r.status_code == 429
    assert "Retry-After" in r.headers


def test_ws_rate_limited_connection_closed(client):
    from fastapi import WebSocketDisconnect

    from app.api.ratelimit import SlidingWindowLimiter

    client.app.state.rate_limiter = SlidingWindowLimiter(limit=1, window_seconds=60)

    # First connection is within budget and streams a normal turn.
    with client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as ws:
        ws.send_json({"session_id": "s1", "message": "hi"})
        assert ws.receive_json()["type"] == "token"
        assert ws.receive_json()["type"] == "done"

    # Second connection exceeds the budget: error frame, then close 1013.
    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as ws2,
    ):
        ws2.receive_json()  # the {"type":"error"} frame
        ws2.receive_json()  # surfaces the close -> WebSocketDisconnect
    assert exc_info.value.code == 1013


# --- Subprotocol auth + error-frame branches ---------------


def test_ws_accepts_subprotocol_api_key(client):
    # The key travels as a Sec-WebSocket-Protocol token — no ?api_key= at all.
    with client.websocket_connect("/ws/chat", subprotocols=[API_KEY]) as ws:
        ws.send_json({"session_id": "s1", "message": "hi"})
        assert ws.receive_json()["type"] == "token"
        done = ws.receive_json()
        assert done["type"] == "done"
        assert done["reply"] == "fake reply"


def test_ws_invalid_payload_closes_1003(client):
    # Malformed JSON -> server sends an error frame, then closes with 1003
    # (Unsupported Data). The second receive surfaces the close as a
    # WebSocketDisconnect.
    from fastapi import WebSocketDisconnect

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as ws,
    ):
        ws.send_text("{not json")
        assert ws.receive_json()["type"] == "error"
        ws.receive_json()  # -> raises WebSocketDisconnect(1003)
    assert exc_info.value.code == 1003


def test_ws_timeout_closes_1013(client, monkeypatch):
    # A graph run that outlives graph_timeout_seconds -> error frame, then
    # close 1013 (Try Again Later). The fake stream sleeps far longer than the
    # patched timeout so asyncio.timeout fires deterministically.
    import asyncio

    from fastapi import WebSocketDisconnect

    class SlowFakeGraph(FakeGraph):
        async def astream_events(self, initial, config=None, version="v2"):
            await asyncio.sleep(1.0)
            if False:
                yield  # never yields a real event before the timeout fires

    monkeypatch.setattr("app.api.routes.settings.graph_timeout_seconds", 0.1)
    client.app.state.graph = SlowFakeGraph()

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect(f"/ws/chat?api_key={API_KEY}") as ws,
    ):
        ws.send_json({"session_id": "s1", "message": "hi"})
        assert ws.receive_json()["type"] == "error"
        ws.receive_json()  # -> raises WebSocketDisconnect(1013)
    assert exc_info.value.code == 1013
