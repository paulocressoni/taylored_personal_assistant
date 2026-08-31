# taylored_personal_assistant

A tailored smart home assistant. A monorepo that runs a Python/FastAPI + LangGraph agentic backend and a React + TypeScript chat frontend (M10, M11). The LLM (DeepSeek via API) is hosted remotely; this repo owns the orchestration, device integrations, API gateway, UI, and deployment. Since M11, every conversation has **persistent memory**: the backend checkpoints each session's history in SQLite, and the frontend lets you create, switch between, and delete multiple conversations.

---

## Table of Contents

1. [Repository structure](#repository-structure)
2. [Prerequisites](#prerequisites)
3. [Step 1 — Install tooling](#step-1--install-tooling)
4. [Step 2 — Set up the Python environment](#step-2--set-up-the-python-environment)
5. [Step 3 — Configure environment variables (secrets)](#step-3--configure-environment-variables-secrets)
6. [Step 4 — Set up VS Code](#step-4--set-up-vs-code)
7. [Step 5 — Install pre-commit hooks](#step-5--install-pre-commit-hooks)
8. [Step 6 — Run the backend](#step-6--run-the-backend)
9. [Step 7 — Run the frontend](#step-7--run-the-frontend)
10. [Docker (M07) — containerized backend](#docker-m07--containerized-backend)
11. [Observability (M08) — self-hosted Langfuse](#observability-m08--self-hosted-langfuse)
12. [HTTP + WebSocket API (M09)](#http--websocket-api-m09)
13. [React frontend (M10) — chat UI](#react-frontend-m10--chat-ui)
14. [LLM provider layer (M02)](#llm-provider-layer-m02)
15. [Language handling (M05)](#language-handling-m05)
16. [Manual path (no uv)](#manual-path-no-uv)
17. [Daily command cheat sheet](#daily-command-cheat-sheet)
18. [Recurrent & on IDE-start commands](#recurrent--on-ide-start-commands)
19. [Troubleshooting](#troubleshooting)
20. [Roadmap](#roadmap)

---

## Repository structure

```
taylored_personal_assistant/
├── .github/
│   └── copilot-instructions.md   # Project constraints for AI tooling
├── .vscode/
│   ├── settings.json             # Interpreter, formatter, test config
│   └── launch.json               # Debug config: "Debug graph CLI" (M08)
├── .pre-commit-config.yaml       # Git hooks (lint, format, secrets)
├── .secrets.baseline             # Detect-secrets allowlist (COMMIT this)
├── .gitignore
├── Makefile                      # Dev workflow: dev-up, test, langfuse-* (M08)
├── README.md
├── docs/
│   ├── docker-cheatsheet.md      # Docker command reference (M07)
│   ├── observability.md          # Self-hosted Langfuse guide (M08)
│   ├── api.md                    # HTTP + WebSocket API reference (M09)
│   └── frontend.md               # React chat UI guide (M10)
├── infra/
│   └── compose/
│       ├── docker-compose.langfuse.yml   # 6-service Langfuse stack (M08)
│       └── .env.example                  # Compose secrets template (M08)
├── backend/
    ├── pyproject.toml            # Project manifest + deps (source of truth)
    ├── uv.lock                   # Locked dependency versions (COMMIT this)
    ├── .python-version           # Pinned Python version (COMMIT this)
    ├── .env.dev / .env.prod      # Real secrets — NEVER commit
    ├── .env.dev.example          # Template for the env files (COMMIT this)
    ├── Dockerfile                # Multi-stage image build (M07)
    ├── .dockerignore             # Keeps the build context lean (M07)
    ├── scripts/                  # Smoke tests + probe clients
    │   ├── smoke_01_chat.py
    │   ├── smoke_02_tools.py
    │   ├── smoke_03_thinking.py
    │   ├── smoke_04_tools_plus_thinking.py
    │   ├── smoke_05_multilingual.py
    │   ├── smoke_06_multilingual_routing.py
    │   ├── smoke_07_langfuse.py  # M08: a CLI run becomes a Langfuse trace
    │   └── ws_probe.py           # M09: WS client to watch token streaming
    ├── tests/                    # Fast unit tests (no network, no LLM)
    │   ├── test_calculator.py
    │   ├── test_config.py
    │   ├── test_langdetect.py
    │   └── test_api.py           # M09/M11: HTTP/WS tests with fakes (graph + checkpointer)
    └── app/                      # Importable Python package ("app")
        ├── __init__.py
        ├── main.py               # FastAPI app + lifespan (M09, M11)
        ├── api/                  # FastAPI serving layer (M09, M11)
        │   ├── routes.py         # /health, /chat, /ws/chat, /sessions/... (M09/M11)
        │   ├── schemas.py        # ChatRequest / ChatResponse / history schemas
        │   └── deps.py           # get_graph + get_checkpointer + config builders
        ├── core/                 # Config, LLM factory, telemetry, observability
        │   ├── __init__.py
        │   ├── callbacks.py      # RunTelemetry callback handler
        │   ├── config.py         # pydantic-settings, loads .env
        │   ├── llm.py            # Role-based LLM factory (ROLE_CONFIG)
        │   └── observability.py  # Langfuse v4 wiring (M08)
        ├── graph/                # LangGraph state + nodes
        │   ├── graph.py          # StateGraph builder (checkpointer-aware, M11)
        │   ├── state.py          # IPAState schema
        │   ├── checkpointer.py   # AsyncSqliteSaver open/setup (M11)
        │   ├── utils.py
        │   └── nodes/            # detect_lang, router, knowledge, ...
        ├── identity/             # (future)
        ├── language/             # Deterministic language handling (M05)
        │   ├── __init__.py
        │   └── detector.py       # lingua detection + fallback chain
        ├── memory/               # (future)
        ├── prompts/              # Leaf prompt modules (no app imports)
        │   ├── knowledge.py
        │   ├── responder.py
        │   └── router.py
        ├── tools/                # Tool registry + calculator
        │   ├── calculator.py
        │   └── registry.py
        └── voice/                # (future)
└── frontend/                    # React + TypeScript chat UI (M10, M11)
    ├── package.json             # Manifest + npm scripts (dev, build, test, lint, types, format)
    ├── package-lock.json        # Locked deps (COMMIT this — npm ci uses it)
    ├── .nvmrc                   # Pinned Node version (24.19.0)
    ├── vite.config.ts           # Vite + Tailwind plugin + dev proxy → :8000
    ├── eslint.config.js         # ESLint flat config (lint, not format)
    ├── .prettierrc.json         # Prettier style (format)
    ├── index.html               # Page shell
    ├── tsconfig*.json           # TypeScript project references
    └── src/
        ├── main.tsx             # React root + <StrictMode>
        ├── App.tsx              # Layout: session sidebar + chat column (M11)
        ├── index.css            # Tailwind v4 entry (@import "tailwindcss")
        ├── api/
        │   ├── types.ts         # GENERATED from OpenAPI (npm run types) — commit it
        │   └── ws.ts            # Hand-written WebSocket frame types
        ├── lib/
        │   ├── deviceId.ts      # Stable UUID in localStorage (M17 alarms)
        │   ├── sessions.ts      # Session registry in localStorage (M11)
        │   ├── history.ts       # Pure message-history mapping (M11, unit-tested)
        │   └── __tests__/       # Vitest unit tests (sessions, history)
        ├── hooks/
        │   ├── useChatStream.ts # WS streaming + sessions + history loading (M10/M11)
        │   └── useAlarmSound.ts # Web Audio stub (wired up in M17)
        └── components/
            ├── SessionList.tsx  # Session sidebar: new / switch / delete (M11)
            ├── MessageList.tsx
            ├── MessageInput.tsx
            ├── TypingIndicator.tsx
            └── LanguageBadge.tsx
```

**Key concepts:**

| Concept | What it is |
|---|---|
| `backend` | The **project/distribution name** (in `pyproject.toml`) — not the import package |
| `app` | The **importable package** (`from app.core.config import settings`) |
| `uv` | Package manager: manages Python, venv, deps, and the lockfile |
| `uv.lock` | Locked, reproducible dependency graph — commit it, never edit by hand |
| `.venv` | Local virtual environment — created by `uv`, gitignored |
| `npm` | JS package manager: manages frontend deps + `package-lock.json` |
| `node_modules` | Frontend deps — created by `npm install`/`npm ci`, gitignored |
| `package-lock.json` | Locked frontend deps — commit it, never edit by hand |
| `Dockerfile` | Multi-stage build: `builder` (uv + deps) → slim `runtime` (venv + code) |
| `.dockerignore` | Excludes `.venv`, caches, `.git` from the Docker build context |
| `infra/compose` | Docker Compose files for local infrastructure (Langfuse stack, M08) |
| `docs/` | Topic guides: Docker (M07), Langfuse (M08), API (M09/M11), frontend (M10/M11) |
| Image vs container | **Image** = immutable blueprint; **container** = a running instance of it |

> The `app` package is installed **editable** into the venv (thanks to `[build-system]` + `[tool.setuptools.packages.find]` in `pyproject.toml`). That is why `from app.core...` imports work from **any** directory — no `PYTHONPATH` hacks needed.

---

## Prerequisites

| Tool | Minimum | Notes |
|---|---|---|
| Git | any recent | Git for Windows |
| `uv` | 0.12+ | Manages Python + venv + deps |
| Python | 3.12 | Managed by `uv` (auto-downloaded if missing) |
| Node.js | 24 (LTS) | Runs the frontend; pinned in `frontend/.nvmrc` |
| npm | 11 | Bundled with Node — frontend package manager |
| Docker | any recent | For the containerized backend (M07) — assumed installed, no setup steps here |
| VS Code | any recent | With the extensions listed in [Step 4](#step-4--set-up-vs-code) |

---

## Step 1 — Install tooling

### 1a. Install `uv` (one-time)

```powershell
winget install --id=astral-sh.uv
```

Close and reopen your terminal so `uv` is on your PATH.

> **Alternative installer** (no winget):
> ```powershell
> powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
> ```

### 1b. Verify

```powershell
uv --version
```

---

## Step 2 — Set up the Python environment

### 2a. Install the pinned Python (3.12)

```powershell
uv python install 3.12
```

The version is already pinned in `backend/.python-version`, so `uv` will always use 3.12 from now on.

### 2b. Create the venv and install dependencies

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
uv sync
```

What this does in one shot:
- Creates `backend/.venv` (if missing)
- Installs every dependency from `pyproject.toml`
- Locks versions into `uv.lock`
- Installs the `app` package **editable** into the venv

### 2c. Verify the editable install works

```powershell
uv run python -c "import app.core.config; print(app.core.config.__file__)"
```

Expected output ends with `...\backend\app\core\config.py`. If it fails, jump to [Troubleshooting](#troubleshooting).

> **Why not activate the venv?** You can, but you don't need to. `uv run` executes inside the project venv automatically and never touches your system Python. If you do want activation (e.g. for interactive `python` in a REPL):
> ```powershell
> .\.venv\Scripts\Activate.ps1
> ```
> If PowerShell blocks it: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`, then retry.

---

## Step 3 — Configure environment variables (secrets)

Secrets are loaded through **pydantic-settings** (`backend/app/core/config.py`), which reads them from your environment and from `.env`.

### 3a. Create your local `.env.dev` (and `.env.prod` for prod)

The app loads **`.env.{ENV}`**, where `ENV` is `dev` or `prod` (set in the OS environment
before the process starts — `make dev-up` sets it for you). The committed templates are
`.env.dev.example` / `.env.prod.example`.

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
Copy-Item .env.dev.example .env.dev
```

Then open `backend/.env.dev` and replace the placeholders with real keys:

```ini
ENV=dev
DEEPSEEK_API_KEY=sk-your-real-key
DEFAULT_TIMEZONE=Europe/Berlin
SUPPORTED_LANGUAGES=["en","de","pt-BR"]

# --- Observability: Langfuse (M08) — optional, best-effort ---
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=pk-lf-your-real-key
LANGFUSE_SECRET_KEY=sk-lf-your-real-key
LANGFUSE_BASE_URL=http://localhost:3000
```

> Langfuse keys are **optional**: if `LANGFUSE_ENABLED` is false or the keys are missing,
> the app runs exactly as before (no observability). Only `DEEPSEEK_API_KEY` is fail-fast.
> See [docs/observability.md](docs/observability.md).

**Rules:**

- `.env.dev` / `.env.prod` are gitignored — real keys never enter the repo. ✅
- `.env.dev.example` / `.env.prod.example` are committed — templates with `REPLACE_ME` placeholders. ✅
- Precedence: real environment variables **override** `.env.{ENV}`, which overrides defaults in `config.py`.
- Never hardcode keys in source code, never `print()` them, and never use interactive `getpass` prompts at import time (they break servers/CI/Docker).

### 3b. Reference keys from code

Always read secrets through the settings object, never `os.getenv` scattered around:

```python
from app.core.config import settings

api_key = settings.deepseek_api_key   # typed, loaded from env/.env
```

---

## Step 4 — Set up VS Code

### 4a. Install recommended extensions

| Extension | ID (install via Marketplace) | Purpose |
|---|---|---|
| Python + Pylance | `ms-python.python` | Language server, interpreter selection |
| Ruff | `astral-sh.ruff` | Linting + formatting |
| Python Debugger | `ms-python.debugpy` | Breakpoints / debugging |
| Even Better TOML | `tamasfe.even-better-toml` | Schema-aware `pyproject.toml` editing |
| Docker | `ms-azuretools.vscode-docker` | For `deploy/` work later |
| ESLint | `dbaeumer.vscode-eslint` | Frontend linting (wires the project's ESLint) |
| Prettier | `esbenp.prettier-vscode` | Frontend formatter (format-on-save) |
| Tailwind CSS IntelliSense | `bradlc.vscode-tailwindcss` | Tailwind class autocomplete + hover docs |
| ES7+ React snippets | `dsznajder.es7-react-js-snippets` | JSX/component snippets |
| Error Lens | `usernamehw.errorlens` | Inline errors/warnings at the line |

### 4b. Confirm the interpreter / formatter selection

The repo ships `.vscode/settings.json` which pins the interpreters and formatters for
both layers (Ruff for Python, Prettier for JS/TS/CSS/HTML/JSON, format-on-save enabled):

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/backend/.venv/Scripts/python.exe",
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true
  },
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["backend/tests"],

  "[javascript]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[javascriptreact]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[typescript]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[typescriptreact]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[css]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[html]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[json]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true },
  "[jsonc]": { "editor.defaultFormatter": "esbenp.prettier-vscode", "editor.formatOnSave": true }
}
```

> **Why `[typescriptreact]`?** In VS Code, `.tsx` has its own language ID distinct from
> `.ts` — without it, Prettier would never format your React components.

To verify in the UI: `Ctrl+Shift+P` → **"Python: Select Interpreter"** → it should show the `backend` venv (Python 3.12). The status bar (bottom-left) displays the active interpreter.

> **If imports are flagged as unresolved:** the venv interpreter isn't selected (VS Code is using a system Python) or the package isn't installed. Fix: run `uv sync` in `backend/`, then re-select the `backend/.venv` interpreter and reload the window.

---

## Step 5 — Install pre-commit hooks

Pre-commit runs linting, formatting, and secret scanning on every commit. The config lives in `.pre-commit-config.yaml` at the repo root.

### 5a. Install the git hook

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

> `pre-commit` is a **dev dependency** in `pyproject.toml`, so `uv sync` already installed it into the venv.
> The `--hook-type commit-msg` activates the Commitizen conventional-commit check.

### 5b. Run it once against everything

```powershell
uv run pre-commit run --all-files
```

The first run downloads hook environments (a minute or two). It runs:
- `ruff` + `ruff-format` — lint and auto-format Python, scoped to `backend/` (may rewrite files — re-stage and commit again)
- `eslint` + `prettier` — lint and auto-format the frontend (`.ts`/`.tsx`/`.js`/`.jsx`/`.css`/`.html`/`.json`). These `local` hooks need `frontend/node_modules` (`cd frontend && npm ci`) and a POSIX shell (Git Bash) on Windows.
- `detect-secrets` — blocks new API keys/tokens; compares against `.secrets.baseline`
- hygiene checks — trailing whitespace, YAML validity, merge-conflict markers, private keys

### 5c. (Re)generate the secrets baseline if needed

The baseline is the allowlist of already-known secrets; it only trips on **new** ones. Regenerate it if you rotate a key:

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant
uvx detect-secrets scan --exclude-files '(\.venv/|\.git/|uv\.lock|backend/\.env)' > .secrets.baseline
```

**Commit `.secrets.baseline`** — it stores hashes (not plaintext) and must be shared so every machine/CI behaves identically.

---

## Step 6 — Run the backend

`app.core.llm` is a pure factory with no `__main__` — to exercise the DeepSeek API, run one of the smoke scripts in `backend/scripts/` (see [LLM provider layer](#llm-provider-layer-m02)):

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run python scripts/smoke_01_chat.py
```

Run tests:

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run pytest                        # fast unit suite (smoke tests deselected)
uv run pytest -m integration         # live DeepSeek API smoke tests
uv run pytest tests/test_api.py      # HTTP + WebSocket API tests (fake graph, no network)
```

Run the dev server (HTTP + WebSocket API, M09):

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant   # from the repo root
make dev-up                  # ENV=dev uv run uvicorn app.main:app --reload
```

Then: `curl.exe http://localhost:8000/health` → `{"status":"ok"}`, or open the interactive
docs at `http://localhost:8000/docs`. Full reference in [docs/api.md](docs/api.md).

---

## Step 7 — Run the frontend

The React chat UI (M10) runs on the Vite dev server at `:5173` and talks to the backend
on `:8000` through a **dev proxy** — the browser never needs to know the backend URL.

### 7a. Install Node (one-time)

```powershell
winget install OpenJS.NodeJS.LTS        # or the installer from nodejs.org
node --version                          # v24.x — pinned in frontend/.nvmrc
```

**Open a new terminal** after installing — the PATH only refreshes in new sessions.

### 7b. Install frontend dependencies

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\frontend
npm install
```

This creates `frontend/node_modules` from `package.json` + `package-lock.json` — the JS
equivalent of `uv sync`.

### 7c. Generate the API types (backend must be running)

```powershell
npm run types    # openapi-typescript http://localhost:8000/openapi.json -o src/api/types.ts
```

Regenerate whenever the backend schemas change, and commit the output (`src/api/types.ts`).

### 7d. Run the dev server

```powershell
npm run dev      # Vite on http://localhost:5173
```

Open `http://localhost:5173`, type a message, and watch tokens stream in. The backend must
be running first (`make dev-up` in another terminal). Full guide: [docs/frontend.md](docs/frontend.md).

---

## Docker (M07) — containerized backend

The backend ships as a **multi-stage Docker image**: a `builder` stage installs dependencies with `uv`, and a slim `runtime` stage copies only the finished venv + source — no build tooling ends up in the final image. See the [Docker cheat sheet](docs/docker-cheatsheet.md) for the full command reference.

### What's in the repo

| File | Purpose |
|---|---|
| `backend/Dockerfile` | Multi-stage build: `builder` (uv + deps) → slim `runtime` (venv + code) |
| `backend/.dockerignore` | Excludes `.venv`, caches, `.git`, `node_modules` from the build context |
| `backend/app/main.py` | FastAPI entrypoint exposing `GET /health` |

### Build the image

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant
docker build -t assistant-backend backend/
```

The build context is `backend/`. Dependency manifests (`pyproject.toml`, `uv.lock`) are copied **before** the app source, so the heavy `uv sync` layer stays cached — code-only edits rebuild in seconds (only `COPY app/ ./app/` re-runs).

### Run it

> The container listens on 8000 **inside its own network**. To reach it from your host, publish the port with `-p 8000:8000` (host port → container port). Without it, `localhost:8000` on your machine finds nothing — the healthcheck still passes because it runs *inside* the container.

```powershell
docker run -d --name assistant-backend -p 8000:8000 assistant-backend
docker ps                   # PORTS: 0.0.0.0:8000->8000/tcp ; STATUS: (healthy)
docker logs assistant-backend
```

Check the health endpoint (on Windows, `curl` is aliased — use `curl.exe`):

```powershell
curl.exe http://localhost:8000/health     # -> {"status":"ok"}
```

### Stop, remove, rebuild

```powershell
docker stop assistant-backend
docker rm assistant-backend

docker build -t assistant-backend backend/                # after code changes
docker run -d --name assistant-backend -p 8000:8000 assistant-backend
```

> Containers are **ephemeral** — anything written inside is lost when the container is removed. Persistent data will live in **volumes** (M08, Compose).
>
> M08 note: this image runs the **assistant** only. Observability (self-hosted Langfuse)
> is a **separate** Compose stack — see [Observability (M08)](#observability-m08--self-hosted-langfuse).

---

## Observability (M08) — self-hosted Langfuse

M08 adds **self-hosted Langfuse v4** for local-dev observability: every graph run becomes
a trace — spans for `detect_lang` / `router` / `knowledge` / `responder`, LLM generations
with token counts, and session grouping. The stack lives in
`infra/compose/docker-compose.langfuse.yml` (6 services: `langfuse-web`,
`langfuse-worker`, `postgres`, `clickhouse`, `redis`, `minio`).

**Best-effort by design**: if Langfuse is off or misconfigured, the assistant runs
identically — only `DEEPSEEK_API_KEY` is fail-fast. The SDK wiring lives in
`backend/app/core/observability.py` and is threaded through every run via
`config["metadata"]` + the Langfuse callback handler.

Quick start (full guide: [docs/observability.md](docs/observability.md)):

```powershell
cd infra/compose
Copy-Item .env.example .env        # fill in secrets (openssl rand -hex 32)
cd ../..
make langfuse-up                   # docker compose up -d --wait
# backend/.env.dev: LANGFUSE_ENABLED=true + pk-lf-* / sk-lf-* keys (see .env.dev.example)
cd backend
ENV=dev uv run python -m scripts.smoke_07_langfuse   # verify a trace lands
# open http://localhost:3000 -> Traces -> newest run
```

Makefile targets: `make langfuse-up`, `make langfuse-down`, `make langfuse-logs`.
Runtime settings live in `backend/app/core/config.py` (`langfuse_*`).

---

## HTTP + WebSocket API (M09)

M09 adds the FastAPI serving layer. The app boots once, compiles the LangGraph graph in a
**lifespan** and stores it on `app.state.graph` (never recompiled per request), pre-warms
the cached chat models, and flushes Langfuse on shutdown. Routes live in `backend/app/api/`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness probe (used by the Docker `HEALTHCHECK`) |
| `POST` | `/chat` | One-shot: run the graph, return `{reply, lang, route}` |
| `WS` | `/ws/chat` | Stream responder tokens, then a `done` frame |
| `GET` | `/sessions/{id}/history` | Read a conversation's persisted messages (M11) |
| `DELETE` | `/sessions/{id}` | Delete a conversation from the checkpointer (M11) |

Quick start (full reference: [docs/api.md](docs/api.md)):

```powershell
make dev-up                          # repo root; uvicorn --reload on :8000
curl.exe http://localhost:8000/health
curl.exe -X POST http://localhost:8000/chat `
  -H "Content-Type: application/json" `
  -d '{"session_id":"s1","message":"hello"}'
# WS streaming — from backend/:
uv run python scripts/ws_probe.py "tell me a short joke"
```

- Input/Output are validated by **Pydantic v2** at the boundary (`schemas.py`) — bad input
  returns `422` before your code runs.
- `POST /chat` runs `graph.ainvoke` (async, matching the M11 `AsyncSqliteSaver`); `WS
  /ws/chat` streams `astream_events(version="v2")`, filtering `on_chat_model_stream`
  events for the `responder` node only.
- **M11 checkpointing:** the graph is compiled with a SQLite checkpointer
  (`app/graph/checkpointer.py`). Every run stamps `config["configurable"]["thread_id"]`
  from `session_id`, so turns sharing an id are merged into one conversation history.
- Each role now carries a per-model `timeout` / `max_tokens` / `max_retries` budget in
  `ROLE_CONFIG` (`app/core/llm.py`).
- Tests (`backend/tests/test_api.py`) exercise the endpoints with duck-typed `FakeGraph`
  + `FakeCheckpointer` — no DeepSeek, no network, no real SQLite.

---

## React frontend (M10) — chat UI

M10 adds a real browser chat UI with **token-by-token streaming** over the `/ws/chat`
WebSocket. Stack: **Vite** (build tool — Create React App is deprecated), **React 19
function components + hooks**, **TypeScript**, **Tailwind CSS v4** (utility classes via
the `@tailwindcss/vite` plugin, no config file). The frontend lives in `frontend/`; the
dev server on `:5173` proxies `/ws` to the backend on `:8000`, so there's no CORS and no
hardcoded backend URL in client code.

Key pieces:

- **`hooks/useChatStream.ts`** — the core hook. Each `sendMessage()` opens a fresh
  `WebSocket` to `/ws/chat` (the backend runs one turn then closes), sends a
  `ChatRequest` envelope, and appends `token` frames to the last assistant message as
  they arrive. Its `useEffect` cleanup closes any in-flight socket — correct under
  React 18/19 `StrictMode`, which deliberately double-invokes effects in dev.
- **Components** — `SessionList` (session sidebar: new/switch/delete), `MessageList`
  (bubbles + auto-scroll), `MessageInput` (Enter to send, disabled while streaming),
  `TypingIndicator` (bouncing dots), `LanguageBadge` (shows the response language from
  the `done` frame: `en` / `de` / `pt-BR`).
- **`lib/deviceId.ts`** — a UUID persisted in `localStorage`, sent as `device_id` with
  every message. Looks unused now, but **M17 (alarms) needs it** to know which browser
  tab/device to ring later.
- **`hooks/useAlarmSound.ts`** — a Web Audio API stub (`play` / `stop`) scaffolded but
  **not wired up**; M17 connects it.
- **Contracts can't drift** — `npm run types` generates `src/api/types.ts` directly from
  the backend's OpenAPI schema (commit it). The WS frame types are hand-written in
  `src/api/ws.ts` because OpenAPI doesn't describe WebSocket traffic.
- **Sessions (M11)** — a session registry in `localStorage` (`lib/sessions.ts`) with a
  sidebar to create, switch, and delete conversations. Each session is a distinct
  `thread_id`, so conversations keep independent checkpointed memory; switching loads a
  session's history via `GET /sessions/{id}/history`; deleting also calls
  `DELETE /sessions/{id}` to erase the server checkpoint.
- **Unit tests (M11)** — Vitest + jsdom (`npm run test`) covering the pure logic in
  `lib/sessions.ts` and `lib/history.ts`.
- **Tooling** — Prettier is the frontend formatter (`.prettierrc.json`, format-on-save in
  `.vscode/settings.json`); pre-commit runs ESLint + Prettier via `local` hooks; CI has a
  `frontend` job (`npm ci` → lint → format check → `tsc -b` + Vite build); Node is pinned
  via `frontend/.nvmrc` and `engines` in `package.json`.

Full file map, walkthrough, and troubleshooting: [docs/frontend.md](docs/frontend.md).

---

## LLM provider layer (M02)

All LLM access goes through the role-based factory in `backend/app/core/llm.py`. Callers pass a *role*; the factory returns a fully configured `ChatDeepSeek`. Model names, temperatures, and thinking mode live only in `ROLE_CONFIG` — business logic never hardcodes a model name.

| Role | Model | Temperature | Thinking | Timeout (s) | Max tokens | Max retries | Use case |
|---|---|---|---|---|---|---|---|
| `router` | `deepseek-v4-flash` | 0.0 | off | 10 | 1024 | 2 | Intent classification / routing |
| `specialist` | `deepseek-v4-flash` | 0.0 | off | 10 | 4096 | 2 | Deterministic tool calling |
| `responder` | `deepseek-v4-flash` | 1.3 | off | 10 | 1024 | 2 | Conversational replies |
| `reasoner` *(planned)* | `deepseek-v4-pro` | 0.0 | on | — | — | — | Planning, diagnosis, disambiguation |

> Each role carries a per-call `timeout`, `max_tokens` budget, and `max_retries` (M09) so a
> slow or hung upstream can never block a thread forever.

**DeepSeek temperature scale** (differs from OpenAI — do not default to 0.7): 0.0 code/math · 1.0 data analysis · 1.3 general conversation · 1.5 creative writing.

### Empirical findings (verified by smoke tests, not docs)

- Thinking mode works on `deepseek-v4-flash` via `extra_body={"thinking": {"type": "enabled"}}` and is **on by default** — so the factory sends `enabled`/`disabled` **explicitly** for every role (a role with thinking off must send `"disabled"`, not merely omit the key).
- `{"thinking": {"type": "disabled"}}` is honored — verified in both directions.
- Reasoning tokens are billed as **output tokens** (~90% of output in the smoke test) — the reason thinking stays off for cheap, high-volume roles.
- Prompt caching works: `usage_metadata.input_token_details.cache_read` reports cached input tokens. Keep stable content (system prompt, tool schemas) **first** in a prompt and volatile content (memory, timestamps, the user message) **last** — cached input is ~50x cheaper than uncached.
- **`smoke_04` — tools + thinking combined:** works on `deepseek-v4-flash`. In the recorded run the model emitted the tool call (`GetWeather` → `location: Berlin`) *and* brief reasoning (19 reasoning tokens), with empty `content`; `cache_read: 256` showed the tool schema served from cache. The combo is undocumented upstream but functional here.

### Running the smoke tests

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run python scripts/smoke_01_chat.py            # standalone, prints usage_metadata
uv run pytest -m integration                      # all 6 as pytest integration tests
uv run pytest                                     # fast unit suite (smoke excluded)
```

Each smoke script verifies one capability: basic chat (`smoke_01`), tool calling (`smoke_02`), thinking mode (`smoke_03`), tools + thinking (`smoke_04`), multilingual round-trip en/de/pt-BR (`smoke_05`), and multilingual graph routing — detect → route → reply in the right language (`smoke_06`).

---

## Language handling (M05)

Multilingual support for English, German, and Brazilian Portuguese is
**deterministic and structured** — language is a first-class value in graph
state (`state["lang"]`), not a side effect of the LLM "happening" to reply in
the right language.

- **Detection** (`app/language/detector.py`) uses `lingua-language-detector`
  restricted to `["en", "de", "pt-BR"]` — free, near-instant, offline, and
  unit-testable (no LLM call, no token spend). A `MIN_CONFIDENCE` threshold
  guards against unreliable guesses on short text.
- **Fallback chain** when confidence is low: previous turn's language →
  the user's stored preference (stub, M13) → `"en"`.
- **Graph wiring**: `START → detect_lang → router`. The `detect_lang` node
  resolves the turn's language into `state["lang"]` before routing, so the
  router, specialists, and responder all read it.
- **Responder policy**: the system prompt says `Reply in {lang}. Never switch
  languages unless the user does.` The router prompt carries German and
  Portuguese few-shot examples so intent routing stays robust for non-English
  input.

---

## Manual path (no uv)

Prefer plain Python? The same setup, manually:

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . pytest ruff pre-commit pydantic-settings fastapi uvicorn[standard] langgraph lingua-language-detector langchain-openai langchain-deepseek langfuse
```

Caveats:
- You lose `uv.lock` reproducibility (use `pip freeze > requirements.txt` instead).
- Bare `pip install` when the venv is **not** activated installs into your **system** Python — always activate first or use `.\.venv\Scripts\python.exe -m pip`.
- The `-e .` editable install still makes `from app.core...` work globally.

---

## Daily command cheat sheet

Run everything from `backend/` unless noted.

| Task | Command |
|---|---|
| Add a dependency | `uv add <package>` |
| Add a dev dependency | `uv add --group dev <package>` |
| Remove a dependency | `uv remove <package>` |
| Install everything to match lock | `uv sync` |
| Run a command in the venv | `uv run <command>` |
| Bump all locked versions | `uv lock --upgrade` |
| Lint + fix | `uv run ruff check --fix .` |
| Format | `uv run ruff format .` |
| Run tests | `uv run pytest` |
| Run the API dev server | `make dev-up` (repo root) · `uv run uvicorn app.main:app --reload` (backend/) |
| Stream a WS chat | `uv run python scripts/ws_probe.py "<message>"` |
| Run the frontend dev server | `cd frontend && npm run dev` (repo root → :5173) |
| Frontend lint | `cd frontend && npm run lint` |
| Frontend format (write / check) | `cd frontend && npm run format` · `npm run format:check` |
| Regenerate API types | `cd frontend && npm run types` (backend on :8000) |
| Install frontend deps (locked) | `cd frontend && npm ci` |
| Langfuse stack up / down / logs | `make langfuse-up` · `make langfuse-down` · `make langfuse-logs` |
| Run pre-commit on everything | `uv run pre-commit run --all-files` |
| Update hook versions | `uv run pre-commit autoupdate` |

---

## Recurrent & on IDE-start commands

The good news: with `uv` there is **no "activate the venv" ritual** — `uv run` handles the environment for you. Still, a few commands matter on a recurring basis. The only real habit is: **run them from `backend/`**.

### Every time you open VS Code / start a terminal

| Situation | Command | Why |
|---|---|---|
| Fresh clone or after `git pull` | `uv sync` | Installs deps to match `uv.lock` — the #1 "it worked before, not now" fix |
| Pre-commit not firing on commit | `uv run pre-commit install` | Re-installs the git hook (once per clone; needed again after some checkouts) |
| Sanity-check the environment | `uv run python -c "import app.core.config; print(app.core.config.__file__)"` | Confirms venv + editable install in one line |
| Fresh clone / after `git pull` (frontend) | `cd frontend && npm ci` | Installs locked frontend deps to match `package-lock.json` |

> You do **not** need to run `uv run pre-commit install` every session — only after a fresh clone or if hooks silently stop firing.

### After pulling new changes (`git pull`)

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant\backend
uv sync                    # sync deps to the new uv.lock
uv run pytest              # make sure everything still passes
```

### After adding/removing dependencies

```powershell
uv add <package>           # updates pyproject.toml + uv.lock + installs
uv remove <package>        # removes everywhere
uv sync                    # apply the change to the venv
```

### Before committing

```powershell
uv run pre-commit run --all-files   # optional full check (hooks also fire on commit)
uv run ruff check .                 # lint backend
uv run ruff format .                # format backend
cd frontend && npm run lint         # lint frontend
cd frontend && npm run format:check # check frontend formatting
```

### Daily development loop

```powershell
cd C:\Users\user_name\Documents\Workspace\taylored_personal_assistant
make dev-up                            # API dev server with --reload (M09)
# ... edit code ...
cd backend && uv run pytest            # run tests
cd backend && uv run ruff check --fix .   # lint + autofix
git add .
git commit                             # pre-commit hooks fire automatically
```

---

**Golden rules:**
1. Always run `uv` commands from `backend/` (or use `--project backend`).
2. Always use `uv run` / `uv add` — never bare `pip install`.
3. Never commit `.env`; always commit `uv.lock`, `.python-version`, `.env.example`, and `.secrets.baseline`.
4. Frontend: never commit `node_modules`; always commit `package-lock.json`, `src/api/types.ts`, and `.nvmrc`; use `npm ci` for reproducible installs.

---

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 1. Core graph & API | LangGraph agent (M02–M06), FastAPI endpoints + WS streaming (M09), pytest suite | ✅ core done |
| 2. Local deployment & DevOps | Docker image (M07), docker-compose, CI/CD → Mini PC | Docker + CI done; compose/CD in progress |
| 3. Observability & memory | Self-hosted Langfuse traces (M08), Qdrant/Chroma vector store | Langfuse done; vector memory future |
| 4. Omni-channel UI | Vite/React chat UI with WS streaming (M10), voice via reSpeaker/ESP32 | chat UI done; voice future |
| 5. Session memory | LangGraph SQLite checkpointing (M11) + frontend multi-session sidebar | ✅ done |
