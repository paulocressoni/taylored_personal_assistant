# HTTP + WebSocket API (M09)

Reference for the FastAPI serving layer added in M09. This is the bridge between the
LangGraph backend and any client (CLI, React frontend (M10), voice devices).

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

> **Config fails fast**: the app will not start without `ENV=dev|prod` and a
> `DEEPSEEK_API_KEY`. Langfuse keys are optional (best-effort observability).

## Endpoints

| Method | Path | Purpose | Returns |
|---|---|---|---|
| `GET` | `/health` | Liveness probe for the Docker HEALTHCHECK | `{"status": "ok"}` |
| `POST` | `/chat` | One-shot: run the graph, return the final answer | `ChatResponse` JSON |
| `WS` | `/ws/chat` | Stream responder tokens, then a `done` frame | `token` frames + `done` |

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
| `session_id` | `str` | required, min 1 char — groups turns for telemetry (Langfuse session) |
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
  -d '{"session_id":"s1","message":"hello"}'
```

**How it works:** Pydantic validates the body at the boundary (a 422 is returned without
your code running). The route then runs the compiled graph via `asyncio.to_thread`
(`graph.invoke` is synchronous/blocking — it would stall the event loop if called
directly), and finally returns the last message plus `lang` / `route` from the final state.

## WS /ws/chat

Protocol: open `ws://localhost:8000/ws/chat`, send **one** JSON envelope (same shape as
`ChatRequest`), then read frames:

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

Try it with the bundled probe client (it prints tokens as they arrive):

```powershell
cd backend
uv run python scripts/ws_probe.py "tell me a short joke"
```

### Consumed by the frontend (M10)

The React chat UI consumes `/ws/chat` directly (see [frontend.md](frontend.md)):

- `frontend/src/api/types.ts` is **generated** from this schema — `cd frontend && npm run
  types` (requires the backend on `:8000`). Commit it so the frontend contract can't
  silently drift.
- The WebSocket **frame types are hand-written** in `frontend/src/api/ws.ts`, because
  OpenAPI documents HTTP only — it does not describe WS traffic.
- The frontend sends the same `ChatRequest` envelope and reads `token` / `done` / `status`
  / `error` frames through the Vite dev proxy (no CORS involved).

## Architecture notes

- **Compile once, reuse forever:** the lifespan builds the graph at boot
  (`app.state.graph = build_graph()`) and pre-warms each role's cached model. The
  compiled graph is stateless — all per-run data lives in the `IPAState` passed to
  `invoke`, so sharing it across concurrent requests is safe.
- **`app/api/deps.py`:**
  - `get_graph(request)` — reads `request.app.state.graph` (what tests swap).
  - `build_initial_state(...)` — seeds the 12-field `IPAState` with a `HumanMessage`.
  - `build_run_config(...)` — threads `RunTelemetry` + the Langfuse handler through the
    run via `config["callbacks"]` / `config["configurable"]` / `config["metadata"]`.
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
