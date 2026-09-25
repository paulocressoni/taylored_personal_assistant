# Docker cheat sheet

Quick reference for the containerized backend (M07). Assumes Docker is **already installed** and the engine is running — no install steps here.

## Is Docker ready?

| Check | Command |
|---|---|
| CLI installed | `docker --version` |
| Engine up | `docker version --format 'client={{.Client.Version}} server={{.Server.Version}}'` |
| End-to-end sanity | `docker run hello-world` |
| List images + size | `docker images` |

> `docker` is just a **client** — the engine lives in Docker Desktop's WSL2 VM. If you get *"cannot connect to the Docker daemon"*, the app isn't running. If `docker` is *"not recognized"*, open a new terminal (PATH) — the machine PATH may have been updated after your terminal started.

## What's needed to start the backend

```powershell
# 1. Build the image (context = backend/)
docker build -t assistant-backend backend/

# 2. Run a container from it (detached, background), publishing port 8000
docker run -d --name assistant-backend -p 8000:8000 assistant-backend

# 3. Verify it's healthy
docker ps                                    # PORTS: 0.0.0.0:8000->8000/tcp ; STATUS: (healthy)
curl.exe http://localhost:8000/health        # -> {"status":"ok"}
```

Rebuild after code changes: repeat step 1, then `docker rm -f assistant-backend` and step 2 again.

> `-p 8000:8000` maps **host port 8000 → container port 8000**. Containers have their own network/`localhost`; without `-p` the host can't reach the app — the healthcheck still passes because it runs *inside* the container.

## Mental model

| Concept | Analogy | Notes |
|---|---|---|
| **Image** | Class / blueprint / recipe | Immutable snapshot: OS + Python + deps + code + start command |
| **Container** | Object / baked cake | A running instance; has a thin writable layer that dies with it |
| **Layer** | Sticky note in a stack | Each Dockerfile instruction = one layer; unchanged layers are `CACHED` on rebuild |
| **Volume** | External USB drive | Persists data beyond a container's lifetime (M08) |

## Build & layer caching

| Command | Purpose |
|---|---|
| `docker build -t <name> <context>` | Build an image tagged `<name>` |
| `docker build --no-cache ...` | Force a full rebuild (ignore cache) |
| `docker image inspect <image>` | Dump image metadata (JSON) |
| `docker history <image>` | Show the layer list |

**Caching rules:**
- A layer is reused only if its inputs *and* everything below it are unchanged.
- Copy **dependency manifests** (`pyproject.toml`, `uv.lock`) before source so installs stay cached.
- Put the code you edit most at the **end** of the Dockerfile.
- **Multi-stage** = build with heavy tools, ship a slim runtime (`FROM ... AS builder`, then copy only the venv into `FROM python:3.12-slim AS runtime`).

## Container lifecycle

| Command | Purpose |
|---|---|
| `docker run -d --name <c> <image>` | Start detached (background) |
| `docker run -it <image> <cmd>` | Start interactive (e.g. `bash`) |
| `docker run -p <host>:<container> <image>` | Publish a port (host → container), e.g. `-p 8000:8000` |
| `docker ps` | List running containers |
| `docker ps -a` | List all containers (incl. stopped) |
| `docker logs <c>` | Show container output |
| `docker stop <c>` | Gracefully stop (container still exists) |
| `docker start <c>` | Restart a stopped container |
| `docker rm <c>` | Delete container **and its writable layer** |
| `docker rm -f <c>` | Force stop + remove in one step |
| `docker exec -it <c> <cmd>` | Run a command inside a *running* container (e.g. `/bin/bash`) |

## Healthchecks

```dockerfile
HEALTHCHECK --interval=30s CMD curl -f http://localhost:8000/health || exit 1
```

- `docker ps` shows `(starting)` → `(healthy)` / `(unhealthy)` in STATUS.
- Exit code 0 = healthy; non-zero = unhealthy.
- On `python:slim`, `curl` is **not** installed — use a Python one-liner instead:
  `HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1`

## Volumes (persistence — M08)

| Command | Purpose |
|---|---|
| `docker volume ls` | List named volumes |
| `docker run -v <name>:/data <image>` | Mount a named volume at `/data` |
| `docker volume rm <name>` | Delete a volume |

Containers are ephemeral: `docker rm` deletes the writable layer. Anything that must survive container death goes in a **volume** or **bind mount**.

## Networks

| Command | Purpose |
|---|---|
| `docker network ls` | List networks |
| `docker network create <name>` | Create a bridge network |
| `docker run --network <name> ...` | Join a network |
| `docker network inspect <name>` | Show members + IPs |


Containers also have their **own** `localhost`. To reach a service from your host, publish its port with `-p <host>:<container>` (e.g. `-p 8000:8000`); otherwise `localhost:<port>` on your machine finds nothing even though the container is healthy.
Inside a compose network, services reach each other **by service name** — never by `localhost`.

## Compose — the full local dev stack (M12)

M12 replaced the M08 "observability only" compose file with **one stack for the whole app**
(backend + frontend + Langfuse + DBs), built from **compose file overlays**:

| File | Role |
|---|---|
| `infra/compose/docker-compose.base.yml` | **Base** — every service in every environment (backend, frontend, the M08 Langfuse stack). No host ports here. |
| `infra/compose/docker-compose.dev.yml` | **Dev overlay** — bind mounts for hot reload, `uvicorn --reload`, the Vite dev server, and the host ports (8000 / 5173 / 3000). |
| `infra/compose/docker-compose.prod.yml` | **Prod overlay** — pulls the GHCR-published images pinned to `:${APP_VERSION}` (never `latest`); the frontend's nginx publishes the browser port and proxies API/WS to the backend; adds the `app-db` Postgres (graph checkpointer) and brings up the full Langfuse stack via `--profile observability`. |

**Environment files (two-env split):** `infra/compose/.env.dev` and
`infra/compose/.env.prod` hold each environment's secrets/knobs — both are
copied from their matching template (`.env.dev.example` / `.env.prod.example`).
Always pass the matching one with `--env-file`
(`.env.dev` for dev, `.env.prod` for prod); the two never share one `.env`.

**How overlays merge** (Compose `-f` rules): later files **override** scalars
(`command`, …), `environment` maps merge per key, and `ports` / `volumes` lists
**concatenate** (never replaced).

**Profiles:** all six Langfuse services are tagged `profiles: ["observability"]`. Pass
`--profile observability` to include them; omit it for a lightweight stack. The backend
depends on `langfuse-web` with `required: false`, so it starts fine without them. Dev
normally runs WITH the profile; prod ALWAYS runs with it (see the prod commands below).

**Image tags:** each service builds and tags `taylored-assistant-<service>:${APP_VERSION:-latest}`.
`APP_VERSION` is derived from `backend/app/_version.py` by the Makefile (`make dev-up`);
unset → falls back to `latest`.

**Graph checkpointer (dev vs prod):** dev persists LangGraph state in a SQLite file on the
`checkpoints_data` named volume — that mount lives in the dev overlay only. Prod replaces
SQLite with the **`app-db` Postgres service** (prod overlay only, own named volume): the
backend gets `CHECKPOINT_DB_URL` pointing at `app-db` and the async Postgres checkpointer
runs its idempotent `setup()` at boot, gated by `depends_on: app-db (healthy)`. `app-db`
is exactly the database the nightly backup dumps (`pg_dump` of the `assistant` DB).

### Published images (GHCR)

On every tagged release (`vX.Y.Z`), CI builds, Trivy-scans (fails on fixable
HIGH/CRITICAL), and pushes **two images** to GitHub Container Registry:

| Image | Contains |
|---|---|
| `ghcr.io/paulocressoni/taylored-personal-assistant-backend` | FastAPI backend |
| `ghcr.io/paulocressoni/taylored-personal-assistant-frontend` | nginx serving the built UI + reverse-proxying `/ws`, `/sessions`, `/health`, `/openapi.json` to the backend |

Each image is tagged `X.Y.Z` (from the git tag), `sha-<sha>`, and `latest`.
**Golden rule:** the git tag IS the image tag IS the deployed version — never deploy `:latest`.

> **Frontend auth key is baked at build time (M23):** the SPA must present the backend's
> shared `ASSISTANT_API_KEY`, but Vite only inlines `VITE_*` vars at build time and
> `frontend/.dockerignore` keeps `.env*` out of the build context. So the publish workflow
> injects `VITE_API_KEY` as a build ARG from the **`VITE_API_KEY` GitHub repository secret**
> (Settings → Secrets and variables → Actions), which must hold the same value as the
> backend's `ASSISTANT_API_KEY`. Key changed? Update the secret **before** cutting the next
> release tag, then re-pull the new `frontend:X.Y.Z`.

```powershell
# Pull a release (requires GHCR auth; images are private by default)
docker login ghcr.io -u paulocressoni
docker pull ghcr.io/paulocressoni/taylored-personal-assistant-backend:0.3.0
docker pull ghcr.io/paulocressoni/taylored-personal-assistant-frontend:0.3.0

# Run the production stack. `-p taylored-assistant-prod` gives prod its OWN
# compose project -> separate volumes/networks/containers from dev
# (taylored-assistant-dev), so conversation data is never shared. Both can
# even run at once (prod publishes :8080; dev uses 8000/5173/3000).
#
# Since the prod overlay gained observability, prod runs the FULL stack:
# app images + the Langfuse profile (`--profile observability`) + the
# `app-db` Postgres that backs the production graph checkpointer. Secrets
# come from `.env.prod`. All services carry `restart: unless-stopped`, so a
# full DeskMini reboot brings the whole stack back by itself.
$env:APP_VERSION = '0.4.1-test'   # the tag you deployed (git tag == image tag, never :latest)
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod --profile observability pull
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod --profile observability up -d --no-build --wait
# open http://localhost:8080  (nginx) — chat streams over /ws through the proxy
#     http://<host>:3000  Langfuse UI (port published by the prod overlay;
#                         see infra/compose/.env.prod, infra/observability/dashboards.md
#                         and infra/observability/alerts.md)
# after a reboot, confirm everything came back: `docker compose ... ps` shows all "Up"
```

### One command (Linux / macOS — `make`, repo root)

| Target | Purpose |
|---|---|
| `make dev-up` | **Full stack** incl. Langfuse (`up --build -d --wait`) |
| `make dev-up-light` | Same stack **without** Langfuse (faster, less RAM) |
| `make dev-down` | Stop the stack (volumes kept) |
| `make dev-logs` | Tail all logs (`-f`) |
| `make dev-ps` | Show running services |
| `make dev-local` | Pre-M12 loop: local uvicorn, no Docker |

### The same commands in PowerShell (Windows)

```powershell
# Full stack incl. Langfuse (the M12 command). DEV runs as its own project
# `taylored-assistant-dev` (-p) so its volumes never mix with prod.
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev --profile observability up --build -d --wait

# Lightweight stack without Langfuse
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev up --build -d --wait

# Status / logs / stop
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev ps
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev logs -f
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev --profile observability down
```

Shortcut — set once per terminal, then plain `docker compose` works:

```powershell
$env:COMPOSE_FILE = "infra/compose/docker-compose.base.yml;infra/compose/docker-compose.dev.yml"
$env:COMPOSE_ENV_FILES = "infra/compose/.env.dev"
$env:COMPOSE_PROJECT_NAME = "taylored-assistant-dev"   # isolate dev from prod
docker compose --profile observability up --build -d --wait
```

### What you should see

| URL | What |
|---|---|
| `http://localhost:5173` | Chat UI (Vite dev server, HMR) |
| `http://localhost:8000/health` | Backend → `{"status":"ok","version":"0.2.0"}` |
| `http://localhost:8000/docs` | FastAPI interactive docs |
| `http://localhost:3000` | Langfuse UI (observability profile only) |

- **Secrets** are split by environment: `infra/compose/.env.dev` (dev) and
  `infra/compose/.env.prod` (prod), each copied from its matching template
  (`.env.dev.example` / `.env.prod.example`) (generate
  `SALT` / `ENCRYPTION_KEY` / `NEXTAUTH_SECRET` with `openssl rand -hex 32`; prod needs
  its OWN fresh values, never the dev ones). Backend secrets come from `backend/.env.dev`
  (referenced via `env_file` in `dev.yml`).
- Compose's `.env` parser treats `#` as a comment **only at line start** — keep comments
  on their own lines.
- Inside the stack, services talk to each other **by service name** (`backend`,
  `frontend`, `app-db`, `langfuse-web`, `clickhouse`, `postgres`, …) — never
  `localhost`. Only browser-facing URLs (`NEXTAUTH_URL`, the published ports) use
  `localhost`.
- **Bind mounts** (dev) = live code, hot reload; **named volumes** (`postgres_data`,
  `clickhouse_data`, `clickhouse_logs`, `redis_data`, `minio_data`) = persistent data
  across restarts.
- **Port conflict on 3000?** An older stack (e.g. the M08 `langfuse-dev` project) is
  still holding the port — stop it: `docker compose -p langfuse-dev down`.
- **Langfuse first login** (headless-init admin, created once on first boot): dev
  `dev@localhost.local` / `dev-password-123` (`.env.dev`); prod `admin@home.local` / the
  `LANGFUSE_INIT_USER_PASSWORD` from `.env.prod` (`prod.yml` defaults; overridable via
  `LANGFUSE_INIT_USER_EMAIL` / `LANGFUSE_INIT_USER_NAME`). See
  [observability.md](observability.md#verify-it-works).

Full observability guide: [observability.md](observability.md).

## Running commands inside a service (compose exec)

`docker compose exec` runs a command in a *running* service container without
a shell on the host. Add `-T` when the command is driven by a script or cron
(no TTY attached). Examples below use the prod project; swap the project /
`--env-file` for the dev one.

```powershell
# Interactive shell in the backend container
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod exec backend sh

# Dump the checkpointer DB to stdout (this is what the nightly backup runs)
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod exec -T app-db pg_dump -U assistant assistant

# Ask ClickHouse (Langfuse's store) for a ground-truth number
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod exec -T clickhouse clickhouse-client --query "SHOW TABLES"
```

## Cleanup

| Command | Purpose |
|---|---|
| `docker image prune` | Delete dangling (untagged) images |
| `docker system prune` | Remove unused images/containers/networks (careful) |
| `docker system df` | Disk usage breakdown |

Project-level cleanup (PowerShell — the `make` equivalents are `dev-down` / `dev-down -v`):

```powershell
# stop the dev stack, keep named volumes
# (dev runs as its own project, taylored-assistant-dev)
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev down
# ...or also delete the volumes (wipes the DB/trace data)
docker compose -p taylored-assistant-dev -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.dev.yml --env-file infra/compose/.env.dev down -v

# Same for the prod project — pass `--profile observability` so the Langfuse
# services started under that profile are stopped too. Add `-v` to also wipe
# the prod named volumes (checkpointer + Langfuse data — which is why the
# nightly backups exist).
docker compose -p taylored-assistant-prod -f infra/compose/docker-compose.base.yml -f infra/compose/docker-compose.prod.yml --env-file infra/compose/.env.prod --profile observability down
```

**Full wipe — everything on the machine** (containers, volumes, images, networks):

```powershell
docker rm -f (docker ps -aq)          # stop + remove ALL containers
docker system prune -a --volumes -f   # remove all unused images/volumes/networks + build cache
```

> On Linux/macOS use `$(docker ps -aq)` instead of PowerShell's `(docker ps -aq)`.

## Network stuck — "Resource is still in use"

If `docker compose ... down` (or a manual `docker network rm`) reports:

```
! Network taylored-assistant_default Resource is still in use
```

…or the longer form:

```
network taylored-assistant_default has active endpoints (name:"taylored-assistant-postgres-1", ...)
```

…it means at least one container is **still attached** to the project's default network.
A network can only be deleted when **zero** containers (running or stopped) are connected
to it — the attached containers are the network's "endpoints".

> **Observed quirk (Compose v5.4.0):** `docker compose down` can skip the container-removal
> step entirely and jump straight to `Network ... Removing`, so it never frees the network —
> even though `docker compose ps` still lists the containers. If `down` keeps failing with
> this message, don't loop on it; remove the containers directly:

```powershell
# 1. Stop + remove every project container (force = the reliable fallback)
docker rm -f (docker ps -a -q -f name=taylored-assistant)

# 2. The network is now free — delete it (or `docker network prune -f` for all unused)
docker network rm taylored-assistant_default
```

- `docker rm` **never** deletes named volumes — `postgres_data`, `clickhouse_data`, etc.
  survive, so the next `up` reuses them.
- To also wipe the volumes: `docker compose ... down -v` (after the containers are gone).

## Common gotchas
- **`curl: (7) Failed to connect to localhost:8000`** → the container is fine, but its port isn't published; re-run with `-p 8000:8000` (the healthcheck passes even when the host can't reach it, because it runs inside the container).

- **`docker` not recognized** → new terminal needed (PATH) or Docker isn't installed.
- **"Cannot connect to the daemon"** → Docker Desktop isn't running; start it and wait for *Engine running*.
- **`curl: not found` in HEALTHCHECK** → not present in `python:slim`; use the Python urllib one-liner.
- **Tag doesn't exist** → verify first: `docker manifest inspect <image>:<tag>` errors out if the tag is wrong.
- **`ModuleNotFoundError: app` in the container** → run via `python -m uvicorn app.main:app` so the container's cwd is on `sys.path` (a bare `uvicorn` console script won't find the copied `app/` package).
- **Slow rebuilds** → check Dockerfile ordering: dependency install must come *before* `COPY app/ ./app/`.
