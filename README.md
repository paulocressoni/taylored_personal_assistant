# taylored_personal_assistant

A tailored smart home assistant. A monorepo that runs a Python/FastAPI + LangGraph agentic backend, with a TypeScript frontend to be added later. The LLM (DeepSeek via API) is hosted remotely; this repo owns the orchestration, device integrations, API gateway, and deployment.

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
9. [LLM provider layer (M02)](#llm-provider-layer-m02)
10. [Manual path (no uv)](#manual-path-no-uv)
11. [Daily command cheat sheet](#daily-command-cheat-sheet)
12. [Recurrent & on IDE-start commands](#recurrent--on-ide-start-commands)
13. [Troubleshooting](#troubleshooting)
14. [Roadmap](#roadmap)

---

## Repository structure

```
taylored_personal_assistant/
├── .github/
│   └── copilot-instructions.md   # Project constraints for AI tooling
├── .vscode/
│   └── settings.json             # Interpreter, formatter, test config
├── .pre-commit-config.yaml       # Git hooks (lint, format, secrets)
├── .secrets.baseline             # Detect-secrets allowlist (COMMIT this)
├── .gitignore
├── README.md
└── backend/
    ├── pyproject.toml            # Project manifest + deps (source of truth)
    ├── uv.lock                   # Locked dependency versions (COMMIT this)
    ├── .python-version           # Pinned Python version (COMMIT this)
    ├── .env                      # Real secrets — NEVER commit
    ├── .env.example              # Template for .env (COMMIT this)
    ├── scripts/                  # LLM smoke tests (pytest "integration" marker)
    │   ├── smoke_01_chat.py
    │   ├── smoke_02_tools.py
    │   ├── smoke_03_thinking.py
    │   ├── smoke_04_tools_plus_thinking.py
    │   └── smoke_05_multilingual.py
    ├── tests/                    # Fast unit tests (no network)
    │   └── test_config.py
    └── app/                      # Importable Python package ("app")
        ├── __init__.py
        └── core/
            ├── __init__.py
            ├── config.py         # pydantic-settings, loads .env
            └── llm.py            # Role-based LLM factory (ROLE_CONFIG)
```

**Key concepts:**

| Concept | What it is |
|---|---|
| `backend` | The **project/distribution name** (in `pyproject.toml`) — not the import package |
| `app` | The **importable package** (`from app.core.config import settings`) |
| `uv` | Package manager: manages Python, venv, deps, and the lockfile |
| `uv.lock` | Locked, reproducible dependency graph — commit it, never edit by hand |
| `.venv` | Local virtual environment — created by `uv`, gitignored |

> The `app` package is installed **editable** into the venv (thanks to `[build-system]` + `[tool.setuptools.packages.find]` in `pyproject.toml`). That is why `from app.core...` imports work from **any** directory — no `PYTHONPATH` hacks needed.

---

## Prerequisites

| Tool | Minimum | Notes |
|---|---|---|
| Git | any recent | Git for Windows |
| `uv` | 0.12+ | Manages Python + venv + deps |
| Python | 3.12 | Managed by `uv` (auto-downloaded if missing) |
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
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
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

### 3a. Create your local `.env`

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
Copy-Item .env.example .env
```

Then open `backend/.env` and replace the placeholders with real keys:

```ini
DEEPSEEK_API_KEY=sk-your-real-key
OPENAI_API_KEY=sk-your-real-key
```

**Rules:**

- `.env` is gitignored — real keys never enter the repo. ✅
- `.env.example` is committed — it's the template with `REPLACE_ME` placeholders. ✅
- Precedence: real environment variables **override** `.env`, which overrides defaults in `config.py`.
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

### 4b. Confirm the interpreter selection

The repo ships `.vscode/settings.json` which pins the interpreter and formatter:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/backend/.venv/Scripts/python.exe",
  "[python]": {
    "editor.defaultFormatter": "charliermarsh.ruff",
    "editor.formatOnSave": true
  },
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["backend/tests"]
}
```

To verify in the UI: `Ctrl+Shift+P` → **"Python: Select Interpreter"** → it should show the `backend` venv (Python 3.12). The status bar (bottom-left) displays the active interpreter.

> **If imports are flagged as unresolved:** the venv interpreter isn't selected (VS Code is using a system Python) or the package isn't installed. Fix: run `uv sync` in `backend/`, then re-select the `backend/.venv` interpreter and reload the window.

---

## Step 5 — Install pre-commit hooks

Pre-commit runs linting, formatting, and secret scanning on every commit. The config lives in `.pre-commit-config.yaml` at the repo root.

### 5a. Install the git hook

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
uv run pre-commit install
```

> `pre-commit` is a **dev dependency** in `pyproject.toml`, so `uv sync` already installed it into the venv.

### 5b. Run it once against everything

```powershell
uv run pre-commit run --all-files
```

The first run downloads hook environments (a minute or two). It runs:
- `ruff` + `ruff-format` — lint and auto-format Python (may rewrite files — re-stage and commit again)
- `detect-secrets` — blocks new API keys/tokens; compares against `.secrets.baseline`
- hygiene checks — trailing whitespace, YAML validity, merge-conflict markers, private keys

### 5c. (Re)generate the secrets baseline if needed

The baseline is the allowlist of already-known secrets; it only trips on **new** ones. Regenerate it if you rotate a key:

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant
uvx detect-secrets scan --exclude-files '(\.venv/|\.git/|uv\.lock|backend/\.env)' > .secrets.baseline
```

**Commit `.secrets.baseline`** — it stores hashes (not plaintext) and must be shared so every machine/CI behaves identically.

---

## Step 6 — Run the backend

`app.core.llm` is a pure factory with no `__main__` — to exercise the DeepSeek API, run one of the smoke scripts in `backend/scripts/` (see [LLM provider layer](#llm-provider-layer-m02)):

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run python scripts/smoke_01_chat.py
```

Run tests:

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run pytest                  # fast unit suite (smoke tests deselected)
uv run pytest -m integration   # live DeepSeek API smoke tests
```

Run the dev server (once `backend/app/main.py` exists):

```powershell
uv run uvicorn app.main:app --reload
```

---

## LLM provider layer (M02)

All LLM access goes through the role-based factory in `backend/app/core/llm.py`. Callers pass a *role*; the factory returns a fully configured `ChatDeepSeek`. Model names, temperatures, and thinking mode live only in `ROLE_CONFIG` — business logic never hardcodes a model name.

| Role | Model | Temperature | Thinking | Use case |
|---|---|---|---|---|
| `router` | `deepseek-v4-flash` | 0.0 | off | Intent classification / routing |
| `specialist` | `deepseek-v4-flash` | 0.0 | off | Deterministic tool calling |
| `responder` | `deepseek-v4-flash` | 1.3 | off | Conversational replies |
| `reasoner` *(planned)* | `deepseek-v4-pro` | 0.0 | on | Planning, diagnosis, disambiguation |

**DeepSeek temperature scale** (differs from OpenAI — do not default to 0.7): 0.0 code/math · 1.0 data analysis · 1.3 general conversation · 1.5 creative writing.

### Empirical findings (verified by smoke tests, not docs)

- Thinking mode works on `deepseek-v4-flash` via `extra_body={"thinking": {"type": "enabled"}}` and is **on by default** — so the factory sends `enabled`/`disabled` **explicitly** for every role (a role with thinking off must send `"disabled"`, not merely omit the key).
- `{"thinking": {"type": "disabled"}}` is honored — verified in both directions.
- Reasoning tokens are billed as **output tokens** (~90% of output in the smoke test) — the reason thinking stays off for cheap, high-volume roles.
- Prompt caching works: `usage_metadata.input_token_details.cache_read` reports cached input tokens. Keep stable content (system prompt, tool schemas) **first** in a prompt and volatile content (memory, timestamps, the user message) **last** — cached input is ~50x cheaper than uncached.
- **`smoke_04` — tools + thinking combined:** works on `deepseek-v4-flash`. In the recorded run the model emitted the tool call (`GetWeather` → `location: Berlin`) *and* brief reasoning (19 reasoning tokens), with empty `content`; `cache_read: 256` showed the tool schema served from cache. The combo is undocumented upstream but functional here.

### Running the smoke tests

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
$env:ENV="dev"
uv run python scripts/smoke_01_chat.py            # standalone, prints usage_metadata
uv run pytest -m integration                      # all 5 as pytest integration tests
uv run pytest                                     # fast unit suite (smoke excluded)
```

Each smoke script verifies one capability: basic chat (`smoke_01`), tool calling (`smoke_02`), thinking mode (`smoke_03`), tools + thinking (`smoke_04`), and multilingual round-trip en/de/pt-BR (`smoke_05`).

---

## Manual path (no uv)

Prefer plain Python? The same setup, manually:

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e . pytest ruff pre-commit pydantic-settings fastapi uvicorn[standard] langgraph langchain-openai langchain-deepseek
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

> You do **not** need to run `uv run pre-commit install` every session — only after a fresh clone or if hooks silently stop firing.

### After pulling new changes (`git pull`)

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
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
uv run ruff check .                 # lint everything
uv run ruff format .                # format everything
```

### Daily development loop

```powershell
cd C:\Users\paulo\Documents\Workspace\taylored_personal_assistant\backend
uv run uvicorn app.main:app --reload   # dev server (once app/main.py exists)
# ... edit code ...
uv run pytest                          # run tests
uv run ruff check --fix .              # lint + autofix
git add .
git commit                             # pre-commit hooks fire automatically
```

---

**Golden rules:**
1. Always run `uv` commands from `backend/` (or use `--project backend`).
2. Always use `uv run` / `uv add` — never bare `pip install`.
3. Never commit `.env`; always commit `uv.lock`, `.python-version`, `.env.example`, and `.secrets.baseline`.

---

## Roadmap

| Phase | Scope |
|---|---|
| 1. Core graph & API | LangGraph agent, FastAPI endpoints, pytest suite *(in progress)* |
| 2. Local deployment & DevOps | Docker image, docker-compose, CI/CD → Mini PC |
| 3. Observability & memory | Langfuse traces, Qdrant/Chroma vector store |
| 4. Omni-channel UI | Vite/React frontend, WebSockets, voice via reSpeaker/ESP32 |
