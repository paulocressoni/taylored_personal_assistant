# HTTP + WebSocket API (M09, M11)

Reference for the FastAPI serving layer added in M09 and extended in M11 (checkpointing).
This is the bridge between the LangGraph backend and any client (CLI, React frontend
(M10/M11), voice devices).

## What M09 added

- `FastAPI` app (`backend/app/main.py`) with a **lifespan** that compiles the LangGraph
  graph **once** and stores it on `app.state.graph`, pre-warms the `@cache`d chat models,
  and flushes Langfuse on shutdown.
- `GET /health` — liveness probe used by the Docker `HEALTHCHECK`.
- `POST /chat` — one-shot request/response: run the graph, return the final answer.
- `WS /ws/chat` — token streaming over WebSocket.
- Pydantic v2 request/response schemas (`backend/app/api/schemas.py`).
- Dependency helpers (`backend/app/api/deps.py`) + HTTP-layer tests with a **fake graph**
  (`backend/tests/test_api.py`) — no network, no DeepSeek key.

## What M11 added

- **Conversation checkpointing**: the graph is compiled with a SQLite-backed checkpointer
  (`AsyncSqliteSaver`, `backend/app/graph/checkpointer.py`) created once in the lifespan
  and stored on `app.state.checkpointer`. Every run carries
  `config["configurable"]["thread_id"] = session_id`, so turns sharing a `session_id`
  merge into one persisted history — HTTP is stateless, the checkpointer is the memory.
- `GET /sessions/{id}/history` — inspect a conversation's persisted messages.
- `DELETE /sessions/{id}` — erase a conversation (runs `checkpointer.adelete_thread`).
- `POST /chat` switched from sync `graph.invoke` to `graph.ainvoke` (required by the
  async checkpointer).
- `get_checkpointer` dependency + `thread_id` added to `build_run_config` in `deps.py`.

## Running the server

```powershell
# repo root — ENV defaults to dev, uvicorn --reload
make dev-up

# equivalent, from backend/
cd backend
ENV=dev uv run uvicorn app.main:app --reload
```

Then:

```powershell
curl.exe http://localhost:8000/health        # -> {"status":"ok"}
```

Interactive docs (generated from the Pydantic schemas): `http://localhost:8000/docs`.

> **Config fails fast**: the app will not start without `ENV=dev|prod`, a
> `DEEPSEEK_API_KEY`, and an `ASSISTANT_API_KEY` (the shared key every client must send —
> see [Authentication](#authentication)). Langfuse keys are optional (best-effort
> observability).

## Endpoints

| Method | Path | Purpose | Returns |
|---|---|---|---|
| `GET` | `/health` | Liveness probe for the Docker HEALTHCHECK | `{"status": "ok"}` |
| `POST` | `/chat` | One-shot: run the graph, return the final answer | `ChatResponse` JSON |
| `WS` | `/ws/chat` | Stream responder tokens, then a `done` frame | `token` frames + `done` |
| `GET` | `/sessions/{id}/history` | Read a conversation's persisted messages (M11) | `SessionHistoryResponse` |
| `DELETE` | `/sessions/{id}` | Delete a conversation's checkpoints (M11) | `{"session_id", "deleted"}` |

## Authentication

Every endpoint **except `GET /health`** is protected by a **shared API key**: the value of
`ASSISTANT_API_KEY` from `backend/.env.dev` (`settings.assistant_api_key` in
`app.core.config`). Like `DEEPSEEK_API_KEY`, it is fail-fast — the app refuses to boot
without it, so auth can never silently be off.

How you present the key depends on the transport (`backend/app/api/auth.py`):

| Transport | Send it as | Failure without it |
|---|---|---|
| HTTP — `POST /chat`, `GET /sessions/{id}/history`, `DELETE /sessions/{id}` | `X-API-Key: <ASSISTANT_API_KEY>` header | `401 Unauthorized` |
| WebSocket — `WS /ws/chat` | `?api_key=<ASSISTANT_API_KEY>` query parameter | connection closed with code `1008` |

Browsers cannot set headers on a WebSocket handshake, which is why the socket takes the
key as a query parameter instead of a header. Keys are compared in constant time
(`secrets.compare_digest`), so timing attacks can't leak the value.

In every example below, replace `<ASSISTANT_API_KEY>` with the value from your
`backend/.env.dev`.

## POST /chat

Request body — `ChatRequest`:

```json
{
  "session_id": "s1",
  "message": "What is the capital of France?",
  "device_id": null
}
```

| Field | Type | Rules |
|---|---|---|
| `session_id` | `str` | required, min 1 char — the `thread_id`: turns sharing it merge into ONE conversation (M11) |
| `message` | `str` | required, 1–4000 chars — empty/too-long returns **422** before the route runs |
| `device_id` | `str \| null` | optional — which device the turn came from |

Response — `ChatResponse`:

```json
{ "reply": "...", "lang": "en", "route": "knowledge" }
```

| Field | Meaning |
|---|---|
| `reply` | The assistant's final answer (last message in the graph state) |
| `lang` | Resolved language tag, e.g. `"pt-BR"` |
| `route` | Routed intent, e.g. `"knowledge"` / `"responder"` |

Example:

```powershell
curl.exe -X POST http://localhost:8000/chat `
  -H "Content-Type: application/json" `
  -H "X-API-Key: <ASSISTANT_API_KEY>" `
  -d '{"session_id":"s1","message":"hello"}'
```

**How it works:** Pydantic validates the body at the boundary (a 422 is returned without
your code running). The route then runs the compiled graph via `await graph.ainvoke(...)`
(async, matching the M11 `AsyncSqliteSaver` — LangGraph runs the sync node code in a
thread executor internally). `build_run_config` stamps
`config["configurable"]["thread_id"] = session_id`, so this turn is **merged into the
saved history** for that session instead of starting from scratch. Finally it returns the
last message plus `lang` / `route` from the final state.

## WS /ws/chat

Protocol: open `ws://localhost:8000/ws/chat?api_key=<ASSISTANT_API_KEY>` (the WS analog of
the `X-API-Key` header — see [Authentication](#authentication)), send **one** JSON
envelope (same shape as `ChatRequest`), then read frames:

| Frame | Fields | Meaning |
|---|---|---|
| `token` | `{ "type": "token", "content": "<chunk>" }` | One chunk of the final answer |
| `done` | `{ "type": "done", "reply", "lang", "route" }` | Run finished; `reply` = all tokens joined |

- Only `on_chat_model_stream` events tagged `langgraph_node == "responder"` are streamed.
  Internal model calls (router, specialist) are filtered out — clients never see
  classification/tool tokens.
- The connection runs **one turn then closes** (one message per connection by design).
- Error policy: a client leaving mid-run is a `WebSocketDisconnect` and is handled
  silently; any other failure propagates so the server logs the real traceback.

Try it with the bundled probe client (it prints tokens as they arrive). The probe reads
the shared API key from `app.core.config.settings` and appends it to the URI as
`?api_key=` automatically — no manual key needed:

```powershell
cd backend
uv run python scripts/ws_probe.py "tell me a short joke"
```

## Sessions & checkpointing (M11)

HTTP is stateless: each request is independent. The **checkpointer** is the server-side
store that gives conversations memory. It's a SQLite database (`backend/checkpoints.db`,
created at startup) keyed by `thread_id`. Same `thread_id` → same conversation.

### Why `thread_id` lives in `config`, not the state

- The **state** is *what the graph remembers* (`messages`, `lang`, `route`, ...).
- The **`thread_id`** is *which conversation this turn belongs to* — it's in
  `config["configurable"]["thread_id"]`, never in `IPAState`.
- Reducers (`Annotated[list, operator.add]` on `messages`) make history accumulate: each
  new turn's messages are appended to the saved ones, so the model sees the whole
  conversation.

### GET /sessions/{session_id}/history

```powershell
curl.exe -H "X-API-Key: <ASSISTANT_API_KEY>" `
  http://localhost:8000/sessions/demo/history
```

Returns `SessionHistoryResponse` — `{"session_id": "...", "messages": [...]}` oldest
first, or `"messages": []` if the session never ran. Debugging/inspection only; the chat
routes read the same store implicitly.

### DELETE /sessions/{session_id}

```powershell
curl.exe -X DELETE http://localhost:8000/sessions/demo `
  -H "X-API-Key: <ASSISTANT_API_KEY>"
```

Runs `checkpointer.adelete_thread(id)`, deleting every checkpoint + pending write for that
thread — the conversation is truly gone. Idempotent: deleting an unknown id is a harmless
no-op (`{"session_id": ..., "deleted": true}`).

### Resetting everything in dev

Stop the server and delete `backend/checkpoints.db*` (the DB uses WAL mode, so also the
`-wal` / `-shm` files). It's recreated empty on next startup.

### Consumed by the frontend (M10)

The React chat UI consumes `/ws/chat` directly (see [frontend.md](frontend.md)):

- `frontend/src/api/types.ts` is **generated** from this schema — `cd frontend && npm run
  types` (requires the backend on `:8000`). Commit it so the frontend contract can't
  silently drift.
- The WebSocket **frame types are hand-written** in `frontend/src/api/ws.ts`, because
  OpenAPI documents HTTP only — it does not describe WS traffic.
- The frontend sends the same `ChatRequest` envelope and reads `token` / `done` / `status`
  / `error` frames through the Vite dev proxy (no CORS involved).
- The browser authenticates with the same shared key: `VITE_API_KEY`
  (`frontend/.env.local`) is sent as the `X-API-Key` header on the `/sessions` HTTP calls
  and as `?api_key=` on the WebSocket — it must equal the backend's `ASSISTANT_API_KEY`.
- Since M11 the frontend also calls `GET /sessions/{id}/history` (load a conversation) and
  `DELETE /sessions/{id}` (delete it); the Vite proxy forwards `/sessions` too.

## Architecture notes

- **Compile once, reuse forever:** the lifespan builds the graph at boot
  (`app.state.graph = build_graph(checkpointer=...)`) and pre-warms each role's cached
  model. Sharing the compiled graph across concurrent requests is safe — per-run data
  lives in `IPAState`, and conversation memory lives in the checkpointer (keyed by
  `thread_id`), not in the graph.
- **`app/api/deps.py`:**
  - `get_graph(request)` — reads `request.app.state.graph` (what tests swap).
  - `get_checkpointer(request)` — reads `request.app.state.checkpointer` (M11).
  - `build_initial_state(...)` — seeds the 12-field `IPAState` with a `HumanMessage`.
  - `build_run_config(...)` — stamps `config["configurable"]["thread_id"] = session_id`
    (M11) and threads `RunTelemetry` + the Langfuse handler via `config["callbacks"]` /
    `config["configurable"]` / `config["metadata"]`.
- **`Depends` vs `app.state`:** routes currently read `request.app.state.graph` directly
  (so tests swap `app.state.graph`). The alternative — declaring `Depends(get_graph)` —
  would additionally enable `app.dependency_overrides`. Both reach the same object.
- **Timeouts:** every role now carries a per-model `timeout`, `max_tokens` budget, and
  `max_retries` in `ROLE_CONFIG` (`backend/app/core/llm.py`), so a hung upstream cannot
  block a thread forever.

## Testing the API layer (no DeepSeek)

`backend/tests/test_api.py` uses FastAPI's `TestClient` and a duck-typed `FakeGraph`
(implements `invoke` + `astream_events`), injected via `app.state.graph = FakeGraph()`.
The lifespan still runs inside the `with TestClient(app)` context manager, so the real
graph is compiled — but never called, because the fake replaces it before any request.

```powershell
cd backend
ENV=dev uv run pytest tests/test_api.py
```

Covered cases: `/health`, `/chat` happy path + 422 on empty message, and `/ws/chat`
token → `done` ordering.

## File map

| File | Role |
|---|---|
| `backend/app/main.py` | FastAPI app + lifespan (compile graph, pre-warm, flush) |
| `backend/app/api/routes.py` | `/health`, `POST /chat`, `WS /ws/chat` |
| `backend/app/api/schemas.py` | `ChatRequest` / `ChatResponse` (Pydantic v2) |
| `backend/app/api/deps.py` | `get_graph`, `build_initial_state`, `build_run_config` |
| `backend/scripts/ws_probe.py` | Minimal WS client to watch token streaming |
| `backend/tests/test_api.py` | HTTP + WS tests with a fake graph |
