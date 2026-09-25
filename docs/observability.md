# Observability (M08) — self-hosted Langfuse

M08 wires **self-hosted Langfuse v4** (via Docker Compose) into the backend so every
graph run becomes a trace. It is **best-effort by design**: if Langfuse is off or
misconfigured, the assistant behaves exactly as before — only `DEEPSEEK_API_KEY` is
fail-fast.

## What you get

- A trace per graph run: spans for `detect_lang` / `router` / `knowledge` / `responder`
  and tool calls, plus LLM generations with real token counts.
- Session grouping (`session_id`), user id, and the run's own name/tags/metadata
  (`schema_version`, `route`, `lang`, token + prompt-cache counters, tool outcomes and
  the error taxonomies) on every trace.
- A browser UI at `http://localhost:3000`.

## Architecture — the compose stack

Since M12 the 6 Langfuse services live in `infra/compose/docker-compose.base.yml`, tagged
`profiles: ["observability"]` so they are **optional** — see
[docker-cheatsheet.md](docker-cheatsheet.md) for the overlay model (base / dev / prod).
Managed from the repo root:

| Service | Image | Role | Host port |
|---|---|---|---|
| `langfuse-web` | `docker.langfuse.com/langfuse/langfuse:4.30.0` | UI + ingestion API | **3000** |
| `langfuse-worker` | `docker.langfuse.com/langfuse/langfuse-worker:4.30.0` | Background jobs (S3 → ClickHouse) | — |
| `postgres` | `postgres:17.11` | Transactional metadata (users, projects, sessions) | — |
| `clickhouse` | `clickhouse/clickhouse-server:25.12.11.4` | OLAP store for traces/spans/generations | — |
| `redis` | `redis:7.4.11` | Queue + cache | — |
| `minio` | `cgr.dev/chainguard/minio` (digest-pinned) | S3-compatible object storage (events + media) | — |

**The rule:** inside Compose, containers reach each other **by service name**, never
`localhost`. All `*_HOST` / `*_URL` values in the compose file use service names. Only
browser-facing URLs (`NEXTAUTH_URL`) use `localhost`.

## Setup

### 1. Compose secrets — `infra/compose/.env.dev` (prod: `.env.prod`)

```powershell
cd infra/compose
Copy-Item .env.dev.example .env.dev     # then fill in the real secrets
# production box:  Copy-Item .env.prod.example .env.prod
```

Generate the random secrets (SALT, ENCRYPTION_KEY, NEXTAUTH_SECRET must be **64 hex
chars**):

```powershell
openssl rand -hex 32
```

> Compose's `.env` parser treats `#` as a comment **only at line start** — keep comments
> on their own lines, or they become part of the value.

### 2. Backend keys — `backend/.env.dev`

Template: `backend/.env.dev.example`. The SDK runs on the **host**, so its base URL is
the published port:

```ini
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-REPLACE_ME
LANGFUSE_SECRET_KEY=sk-lf-REPLACE_ME
LANGFUSE_BASE_URL=http://localhost:3000
```

> The project's public/secret keys must match the ones the compose stack auto-provisions
> in `infra/compose/.env.dev` (production: `infra/compose/.env.prod`; see
> `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` /
> `LANGFUSE_INIT_PROJECT_SECRET_KEY`).
>
> **Two different base URLs, deliberately:** the SDK run on the **host** uses the
> host-published `http://localhost:3000`; the compose file uses internal service-name
> URLs. Since M12, when the backend runs **inside** the Compose network, `dev.yml`
> overrides `LANGFUSE_BASE_URL` to `http://langfuse-web:3000` (service name) — so the
> value in `backend/.env.dev` only applies to host-native runs (`make dev-local`).

### 3. Start the stack

M12: observability comes up with the whole app. **Linux / macOS** (`make`, repo root):

```bash
make dev-up          # full stack incl. Langfuse (observability profile)
# lighter, no Langfuse:  make dev-up-light
```

**Windows / PowerShell** (full stack):

```powershell
docker compose -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev --profile observability up --build -d --wait
```

| Target / command | Purpose |
|---|---|
| `make dev-up` | Full stack incl. Langfuse (`up --build -d --wait`) |
| `make dev-up-light` | Same stack without Langfuse |
| `make dev-down` | Stop everything (volumes kept) |
| `make dev-logs` | Tail all logs |

## Verify it works

Run the Langfuse smoke test (a CLI graph run that asserts a trace is produced):

```powershell
cd backend
ENV=dev uv run python -m scripts.smoke_07_langfuse
```

Then open `http://localhost:3000` → **Traces** → newest run. You should see an
`assistant:knowledge` trace with spans for `detect_lang` / `router` / `knowledge` /
`responder` / `telemetry` and an LLM generation with token counts. The trace's name,
its tags (`env:dev`, `channel:cli`, `status:*`) and its metadata
(`schema_version = "2.1"`, `route`, `lang`, token counters, `tool_outcomes`, the error
taxonomies) are written by the invoke site AFTER the graph returns — see "How the
wiring works" below. Production dashboards, alerts and the ClickHouse ground-truth
queries live in `infra/observability/`.

**First login** — the headless init (M08) creates the first admin user once, on first
boot:

| Environment | Email | Name | Password (matching env file) |
|---|---|---|---|
| dev | `dev@localhost.local` | `Dev Admin` | `LANGFUSE_INIT_USER_PASSWORD` in `infra/compose/.env.dev` (default `dev-password-123`) |
| prod | `admin@home.local` | `Prod Admin` | `LANGFUSE_INIT_USER_PASSWORD` in `infra/compose/.env.prod` |

The dev email/name are the `docker-compose.base.yml` defaults; the prod overlay
(`docker-compose.prod.yml`) swaps them for `admin@home.local` / `Prod Admin`, both
overridable via `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_NAME` in
`infra/compose/.env.prod`. Langfuse creates the admin only on first boot — set these
before that first start.

## How the wiring works — `backend/app/core/observability.py`

- Langfuse v4 is **OpenTelemetry-based**: the LangChain `CallbackHandler` is constructed
  with no args and binds to the process-wide singleton client.
- Per-trace attributes are passed through the invoke `config["metadata"]` using reserved
  `langfuse_*` keys: `langfuse_session_id`, `langfuse_user_id`, and the STATIC half of the
  trace identity — `langfuse_trace_name` (fallback `assistant:turn`) and `langfuse_tags`
  (`env:*`, `channel:*`). Stamping those at invoke time means a run that crashes, times out
  or is abandoned still lands named and filterable; `enrich_trace` refines the name to
  `assistant:<route>` and merges the `status:*` tag once the run finishes.
- `turn_span()` opens an **app-owned root span** around the graph call at every invoke
  site (HTTP `/chat`, the WebSocket handler, the CLI and the smoke test). Langfuse reads
  a trace's name/tags/metadata from its app-root span, and `propagate_attributes` only
  touches the span that is current — so `enrich_trace(final_state)` runs after the graph
  returns, while that span is still open, and the attributes land on the trace instead
  of being silently dropped.
- The graph's terminal **`telemetry` node** no longer talks to Langfuse: it reconciles
  the run's usage (`llm_calls`, token + prompt-cache totals, error buckets) into
  `IPAState` from the `RunTelemetry` counter in
  `config["configurable"]["run_telemetry"]`, so the persisted state — and the trace
  metadata built from it — reflects every model call in the run.
- `_get_client()` builds the singleton with `environment=settings.env` (so traces land
  in `prod` / `dev` rather than Langfuse's `default`) and `release=__version__`.
- `flush()` (v4: on the client) is called on app shutdown and after the smoke test so
  queued events land before the process exits.
- Everything is gated by `settings.langfuse_ready`; when off, helpers return `None` /
  no-op.

Runtime settings live in `backend/app/core/config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `langfuse_enabled` | `False` | Master switch |
| `langfuse_public_key` | `""` | Project public key (`pk-lf-…`) |
| `langfuse_secret_key` | `""` | Project secret key (`sk-lf-…`) |
| `langfuse_base_url` | `http://localhost:3000` | SDK endpoint (host-published port) |

## Troubleshooting

- **Stack not healthy:** `make dev-logs` (PowerShell: `docker compose -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env logs -f`); check each service with `docker compose -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env ps` (the worker has no healthcheck by design).
- **Traces missing:** confirm `LANGFUSE_ENABLED=true` + matching keys in
  `backend/.env.dev`, and that the stack is up. The SDK path is best-effort — check the
  backend logs for Langfuse errors, not the app failing.
- **Headless init doesn't rerun:** the org/project/admin user are created **once** on
  first boot. If you change the keys in `infra/compose/.env`, reset the stack volumes
  (`docker compose down -v`) to re-provision from scratch.
- **`ENCRYPTION_KEY` invalid:** must be exactly 64 hex characters.
- **Timezone:** Postgres and ClickHouse must be UTC (already set in the compose file).
