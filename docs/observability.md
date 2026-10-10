# Observability (M08) — self-hosted Langfuse

M08 wires **self-hosted Langfuse v4** (via Docker Compose) into the backend so every
graph run becomes a trace. It is **best-effort by design**: if Langfuse is off or
misconfigured, the assistant behaves exactly as before — only `DEEPSEEK_API_KEY` is
fail-fast.

## What you get

- A trace per graph run: spans for `detect_lang` / `router` / `knowledge` / `responder`
  and tool calls, plus LLM generations with real token counts.
- Session grouping (`session_id`), user id, and the run's own name/tags/metadata
  (`schema_version`, `route`, `lang`, `stt_lang` on voice turns, token + prompt-cache
  counters, tool outcomes and the error taxonomies) on every trace.
- A readable **voice trace waterfall**: a spoken turn draws an `stt` span around the
  transcription and one `tts` span per sentence, so the stage timings become intervals you
  can see instead of six numbers you have to trust — see
  "The voice trace waterfall" below.
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
(`schema_version = "2.2"`, `route`, `lang`, token counters, `tool_outcomes`, the error
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
- **Trace input/output** is written by a SECOND SDK call, `update_current_span(input=…,
  output=…)`, because `propagate_attributes` carries only the trace IDENTITY (name, tags,
  metadata) and has **no** input/output parameter — the reason every trace list entry used
  to show blank IO while the LangChain callback's own child chain span held the messages.
  The values come from the pure `turn_io(state)`: the LAST `HumanMessage` (a checkpointed
  thread holds the whole history, so the first one belongs to an earlier turn) and the last
  message's flattened text, each capped at `MAX_TRACE_IO_CHARS` (2000).
- **Failures stay visible.** A crash or a graph timeout never reaches `enrich_trace`, so
  each invoke site's error path calls `mark_turn_failed(detail, channel=…)` while the
  app-root span is still open: the span gets `level=ERROR`, the cause in `status_message`,
  and a `status:failed` trace tag. The `turn_span()` wrapper therefore sits OUTSIDE the
  `asyncio.timeout` at each invoke site — the timeout only surfaces as `TimeoutError`
  once its body has unwound, and the mark has to happen before the span closes.
- **Barge-in is marked, not hidden.** A voice turn cut short by the user is unwound by
  `CancelledError` — a `BaseException`, so a bare `except Exception` never sees it.
  `VoiceSession._run_turn` catches it explicitly and stamps
  `mark_turn_cancelled(..., channel="voice")` (`level=WARNING` + a `barge-in` status
  message + a `status:cancelled` tag) before re-raising. That tag is what makes the
  interruption rate queryable instead of every cut-off turn looking completed.
- The marker helpers rebuild the FULL tag list (`env:*`, `channel:*`, then the marker tag)
  instead of passing only the marker: by the time an error handler runs, the callback
  handler has already exited its propagation context, so `propagate_attributes(tags=…)`
  REPLACES the app-root span's tags — a marker-only list would drop `env:`/`channel:`.
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

### Trace IO and the metadata ordering invariant

Two facts about the app-root span explain both the bug and the fix:

1. Langfuse mirrors a trace's input/output from its **root observation's** input/output.
   `turn_span()` creates that observation with no IO, and `propagate_attributes` cannot
   carry IO, so `enrich_trace` fills it from the pure `turn_io(state)` via
   `update_current_span(input=…, output=…)`. An empty or message-less state yields
   `("", "")` rather than raising, so a failed run is still labelable.
2. `enrich_trace` and `stamp_voice_timing` both write through `propagate_attributes`,
   which writes each metadata key as its OWN span attribute
   (`langfuse.trace.metadata.<key>`) — so the second call ADDS to the first instead of
   replacing it. A voice turn therefore ends up with the run identity (`schema_version`,
   `route`, …) AND the stage timings (`stt_ms`, `llm_first_token_ms`, …) on the same span.
   That ordering is pinned by a unit test: a future refactor to a whole-blob write would
   silently strip the identity from every voice trace.

Runtime settings live in `backend/app/core/config.py`:

| Setting | Default | Purpose |
|---|---|---|
| `langfuse_enabled` | `False` | Master switch |
| `langfuse_public_key` | `""` | Project public key (`pk-lf-…`) |
| `langfuse_secret_key` | `""` | Project secret key (`sk-lf-…`) |
| `langfuse_base_url` | `http://localhost:3000` | SDK endpoint (host-published port) |

### The voice trace waterfall

A voice turn used to be one flat row: `stamp_voice_timing` writes six scalars (`stt_ms`,
`graph_ttft_ms`, `tts_ttfb_ms`, `first_audio_ms`, `tail_ms`, `turn_ms`) onto the app-root
span. That is the right shape for a dashboard — one row per turn, cheap to aggregate — and
the wrong shape for a human: STT and each TTS call are INTERVALS inside the turn, and a
scalar cannot show that the second sentence is being synthesised while the first is still
playing.

So the voice session opens a child span per stage, inside the app-root span:

| Span | Opened | Carries |
|---|---|---|
| `stt` | once per turn, around the transcription call | `input` = `lang=<tag\|auto> audio_ms=<n>`, `metadata` = `provider`, `model`, `audio_ms`, `text_chars`; `output` = the transcript, capped at `MAX_TRACE_IO_CHARS` |
| `tts` | once per SENTENCE, around that sentence's synthesis loop | `input` = the sentence, capped at `MAX_TRACE_IO_CHARS`; `metadata` = `voice`, `model`, `chars`, `index`, `ttfb_ms`, `bytes`, `audio_ms` |

Both come from one helper, `child_observation(name, *, input, metadata)`. It yields the
started observation, so the caller can write values that only exist AFTER the call
(`.update(output=…)`, `.update(metadata=…)`), and it yields `None` when observability is off —
which is why every call site guards. The attribute mapping itself lives in the pure
`stt_attributes` / `tts_attributes`, so it is unit-tested with no stack running, the same
pattern as `trace_attributes`, `timing_metadata` and `turn_io`.

How to read it:

- **Overlap is the point.** On a multi-sentence answer the second `tts` span starts before
  the first ends; the widths are the real synthesis time of each sentence, not a slice of a
  total.
- **`index` is the speaking order** (1-based). Two overlapping spans can start milliseconds
  apart, so timestamps alone cannot say which sentence came first — `index` can.
- **`bytes` and `audio_ms` describe the same buffer** at the session's NEGOTIATED output
  rate (24 kHz for the browser, 16 kHz for the device), counted after resampling. Counting
  the provider's own bytes against the client's rate would report the same audio 1.5× too
  long on a 16 kHz device.
- **The span's `ttfb_ms` and the turn's `tts_ttfb_ms` mark** pick up the same first PCM byte
  from different origins: the span starts when the sentence is handed to synthesis, the mark
  when the first token arrived from the graph, and the SDK's own span duration comes from a
  different clock than `monotonic_now()`. They should agree within a few milliseconds for the
  first sentence; a wide gap is a signal about where the pipeline waited (text held back by
  the splitter, a span opened late), not noise to ignore.
- **`audio_ms` appears twice on an `stt` span, deliberately**: the `input` derives it from the
  submitted PCM, the metadata from the transcript, which the adapter computed from the same
  bytes and rate. In production they are equal; if they ever diverge, the adapter's sample
  rate is wrong — a cross-check that costs nothing.
- **A barge-in leaves the `tts` span in place**, closed where the loop was cut, and the trace
  still carries `status:cancelled`. The cancellation is not swallowed: `CancelledError`
  derives from `BaseException`, so closing the span skips the attributes written after the
  loop and nothing else.

Two invariants to keep in mind before editing this code:

- The child spans are opened and closed INSIDE `VoiceSession._answer`. The failure and
  interruption markers (`mark_turn_failed`, `mark_turn_cancelled`, `stamp_voice_timing`) run
  in `_run_turn` and write through `update_current_span`, which targets whichever observation
  is CURRENT. They are therefore only correct while the app-root span is current — which is
  exactly what closing the children before returning guarantees. A child span opened around
  them would take the `status:failed` marker and the stage timings onto itself and leave the
  crashed turn looking healthy.
- The stage spans add **no trace-level metadata**, so `TRACE_SCHEMA_VERSION` is unchanged
  (`2.2`). The dashboard contract — the six scalars on the root, and the ClickHouse queries
  in `infra/observability/` — is untouched. The waterfall is an extra dimension for a human
  reading one turn, not a schema change.

## Upgrading

Two versions move independently: the **app** (the GHCR image, pinned by
`APP_VERSION`) and the **observability stack** (image tags pinned in
`docker-compose.base.yml`).

### App — a tag bump, nothing else

The git tag IS the image tag, so a release is `APP_VERSION` + the prod targets
(the DeskMini runs Linux, so `make` is available there):

```bash
APP_VERSION=0.5.0 make prod-up     # pull + up -d --no-build --wait
```

On a Windows dev machine `make` needs a POSIX shell, so set `$env:APP_VERSION`
and run the raw `pull` / `up -d --no-build --wait` pair from the usage block at
the top of `infra/compose/docker-compose.prod.yml`.

`--no-build` on purpose: prod runs the published, Trivy-scanned image and never
compiles the working tree. Note that `/health` reports the **source** version
(`backend/app/_version.py`), not the image tag — the two only agree once
release-please bumps that file, so a `-test` tag legitimately shows the previous
released version.

### Observability stack — back up, then bump both Langfuse images

1. **Back up first** (runbook: `infra/backup/BACKUP.md`). Langfuse's Postgres holds
   your dashboards, alerts and project config; ClickHouse holds the traces. An
   upgrade migrates both, so last night's dump is the cheapest possible undo.
2. Bump `langfuse-web` **and** `langfuse-worker` in `docker-compose.base.yml` to the
   **same** version — they cooperate over Redis, and a mismatch shows up as events
   that never finish ingesting.
3. Bump the other pinned images (postgres / clickhouse / redis / minio) only
   deliberately: a ClickHouse upgrade can rewrite parts of `clickhouse_data`, and a
   downgrade is not supported.
4. `make prod-up`. Langfuse applies its Postgres and ClickHouse migrations when
   `langfuse-web` starts.
5. Verify: log in ("First login"), **Traces** shows a new run, and a fresh assistant
   turn appears within a few seconds.

`langfuse-worker` has no healthcheck by design, so `--wait` has nothing to wait on
for it and `ps` shows it as merely `Up`.

## Reboot survival

The DeskMini should behave like an appliance, so the stack has to come back on its
own after a power cut.

- Every service sets `restart: unless-stopped`, so Docker restarts them on boot —
  **provided the daemon itself starts at boot**: `sudo systemctl enable --now docker`
  (verify with `systemctl is-enabled docker`).
- `unless-stopped` means "restart unless *you* stopped it". `make prod-down` runs
  `docker compose down`, which REMOVES the containers (volumes are kept), so after a
  `down` a reboot has nothing to restart until you `make prod-up`. To pause the stack
  and still have it return by itself, use `stop` instead of `down`.
- State lives in **named volumes**, never in the containers: `postgres_data`,
  `clickhouse_data`, `clickhouse_logs`, `redis_data` and `minio_data` for Langfuse,
  plus `app_db_data` for the conversation checkpoints. Rebooting or recreating a
  container never touches them.
- **Every lifecycle command needs `--profile observability`, including the ones that
  STOP things.** Compose only acts on services in the active profile set, so a plain
  `down` removes `backend`, `frontend` and `app-db` and leaves all six Langfuse
  containers running — and `ps` lists them either way, which is what hides the
  mistake. The `Makefile` targets (`dev-down` / `prod-down` / `dev-logs` / `prod-logs`
  / `dev-ps` / `prod-ps`) always pass it, so prefer those; if you type the command
  yourself, pass it too. Leftovers from an earlier profile-less run:
  `... down --remove-orphans` once.
- **Cold-boot ordering is not guaranteed.** `depends_on` is honoured by `up`, not by
  the daemon restarting containers after a reboot, so `backend` can start before
  `app-db` is accepting connections. `open_checkpointer()` runs `setup()` on every
  start, so the lifespan raises and uvicorn exits — `restart: unless-stopped` then
  retries until Postgres is ready. Expect a brief crash-loop in the logs and a healthy
  stack within a minute; if it never settles, check `app-db` health first.

After a reboot, check in this order:

1. `make prod-ps` — every service `Up (healthy)` except `langfuse-worker`.
2. `http://<deskmini>:8080` serves the UI and `/health` answers.
3. `http://<deskmini>:3000` accepts a login.
4. One assistant turn produces a new `assistant:<route>` trace — that single check
   covers backend → Langfuse ingest → ClickHouse end to end.

## Troubleshooting

- **Stack not healthy:** `make dev-logs` to tail the logs (Linux/macOS), or the raw `docker compose … logs -f` from "Start the stack" with `--env-file infra/compose/.env.dev`; check status with the matching `ps`. `langfuse-worker` has no healthcheck by design, so it never reports healthy.
- **Traces missing:** confirm `LANGFUSE_ENABLED=true` + matching keys in
  `backend/.env.dev`, and that the stack is up. The SDK path is best-effort — check the
  backend logs for Langfuse errors, not the app failing.
- **Headless init doesn't rerun:** the org/project/admin user are created **once** on
  first boot. If you change the keys in `infra/compose/.env.dev` (prod: `.env.prod`),
  reset that environment's volumes with `down -v` — this DELETES its Langfuse data —
  to re-provision from scratch.
- **`ENCRYPTION_KEY` invalid:** must be exactly 64 hex characters.
- **Timezone:** Postgres and ClickHouse must be UTC (already set in the compose file).
