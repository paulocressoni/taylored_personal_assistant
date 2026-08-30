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

## Compose & the self-hosted Langfuse stack (M08)

`infra/compose/docker-compose.langfuse.yml` runs a 6-service Langfuse v4 stack for local-dev
observability (`langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `redis`, `minio`).
Managed from the repo root via the Makefile:

| Command | Purpose |
|---|---|
| `make langfuse-up` | Bring up the stack (`docker compose up -d --wait`) |
| `make langfuse-down` | Stop the stack |
| `make langfuse-logs` | Tail logs |

The same commands as plain Compose (what the Makefile wraps):

```powershell
docker compose -f infra/compose/docker-compose.langfuse.yml --env-file infra/compose/.env up -d
docker compose -f infra/compose/docker-compose.langfuse.yml ps
docker compose -f infra/compose/docker-compose.langfuse.yml logs -f
```

- **Secrets** come from `infra/compose/.env` (copy from `.env.example`; generate
  `SALT` / `ENCRYPTION_KEY` / `NEXTAUTH_SECRET` with `openssl rand -hex 32`).
- Compose's `.env` parser treats `#` as a comment **only at line start** — keep comments
  on their own lines.
- The **UI is published on host port 3000**: `http://localhost:3000`.
- Inside the stack, services talk to each other **by service name** (`postgres`,
  `clickhouse`, …) — never `localhost`. Only browser-facing URLs use `localhost`.
- **Volumes** (`postgres_data`, `clickhouse_data`, `clickhouse_logs`, `redis_data`,
  `minio_data`) persist the stack's data across restarts.

Full observability guide: [observability.md](observability.md).

## Cleanup

| Command | Purpose |
|---|---|
| `docker image prune` | Delete dangling (untagged) images |
| `docker system prune` | Remove unused images/containers/networks (careful) |
| `docker system df` | Disk usage breakdown |

## Common gotchas
- **`curl: (7) Failed to connect to localhost:8000`** → the container is fine, but its port isn't published; re-run with `-p 8000:8000` (the healthcheck passes even when the host can't reach it, because it runs inside the container).

- **`docker` not recognized** → new terminal needed (PATH) or Docker isn't installed.
- **"Cannot connect to the daemon"** → Docker Desktop isn't running; start it and wait for *Engine running*.
- **`curl: not found` in HEALTHCHECK** → not present in `python:slim`; use the Python urllib one-liner.
- **Tag doesn't exist** → verify first: `docker manifest inspect <image>:<tag>` errors out if the tag is wrong.
- **`ModuleNotFoundError: app` in the container** → run via `python -m uvicorn app.main:app` so the container's cwd is on `sys.path` (a bare `uvicorn` console script won't find the copied `app/` package).
- **Slow rebuilds** → check Dockerfile ordering: dependency install must come *before* `COPY app/ ./app/`.
