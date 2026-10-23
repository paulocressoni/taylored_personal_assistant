# First production run — the complete checklist

Everything that has to be true **before** `make prod-up` works the first time, in the order it
has to happen. Written from the first real deployment, so it includes the steps that are easy to
forget because they fail *silently* rather than loudly.

After the first run, use [`deploy.md`](deploy.md) for updates and rollbacks. This page is the
one-time setup.

Placeholders: `<host>` is the machine's LAN address or name, `<owner>` the GitHub account that
published the images (this repo: `paulocressoni`), `<tag>` the release you deploy (e.g. `0.4.0`).

---

## 0. Decide the shared values first

Two values must exist in **more than one place**, and a mismatch is not always an error message —
sometimes it is an empty dashboard. Decide them before touching a file, and keep them in a
password manager.

| Value | Where it must match | What happens if it doesn't |
|---|---|---|
| **API key** | `backend/.env.prod` → `ASSISTANT_API_KEY` **and** the GitHub repository secret `VITE_API_KEY` (baked into the frontend image at build) | The UI loads, then every request fails with *"Authentication failed — check VITE_API_KEY"* (WS close 1008 / HTTP 401) |
| **Langfuse project keys** | `infra/compose/.env.prod` → `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` / `_SECRET_KEY` **and** `backend/.env.prod` → `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | No crash and no log error: the project is created with one pair, the SDK sends the other, and **every trace is silently rejected** |

Generate the API key once:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Use URL-safe characters only — the key travels as a WebSocket query parameter
(`?api_key=…`) on the fallback transport.

> **If you are not the repo owner**, you cannot read the existing `VITE_API_KEY` secret. Either
> fork, set your own secret and publish your own images (see the publish workflow), or recover the
> baked value from a published frontend image with the command in §4.2.

---

## 1. Host prerequisites

| Item | Requirement | Check |
|---|---|---|
| OS | 64-bit Linux, any current distro, server or desktop | `uname -m` |
| Docker | Engine ≥ 24 + Compose v2, **enabled at boot** | `docker --version`, `docker compose version`, `systemctl is-enabled docker` |
| Deploy toolchain | `git`, `make`, `curl`, `ca-certificates` | `git --version && make --version && curl --version` |
| Address | **stable** (DHCP reservation or static) — the satellite firmware compiles it in | `ip -4 addr`, then confirm the pin in the router |
| Clock | timezone set, NTP synced, RTC in UTC | `timedatectl` |
| Power | idle suspend disabled (desktop editions) | GNOME: `sleep-inactive-ac-type` must be `'nothing'` |
| Disk | ≥ 100 GB free where `/var/lib/docker` lives | `df -h /var/lib/docker` |
| Ports | `8080` and `3000` free | `ss -tlnH \| grep -E ':(8080\|3000)$'` |

Install Docker from Docker's own repository (never snap — strict confinement breaks host volume
paths), then enable it at boot and add yourself to the group:

```bash
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

> ⚠️ **Group membership only applies to a new login session.** Shells, terminals and editor
> processes started before `usermod` keep their old group set and still get
> `permission denied ... /run/docker.sock`, while `getent group docker` cheerfully lists you.
> Log out and back in (or `newgrp docker` for one shell) and confirm **in the session that will
> run `make`**: `id -nG | grep -x docker`.

Optional but recommended on a host that also serves as a workstation: SSH key-only access
(`openssh-server`), and a firewall allowing SSH plus the LAN range on `8080` only.

---

## 2. Contributor tooling (needed to lint, test and commit — not to run prod)

`make prod-up` itself only needs Docker. Install these if you will also run `make lint`,
`make test`, `make types` or commit to the repo from this machine.

### 2.1 `uv` — pinned to the Dockerfile's version

The backend image pins `uv` (`COPY --from=ghcr.io/astral-sh/uv:<version>`). Install **the same
version** locally: a newer uv can write a lockfile revision the pinned one refuses to read, and
then `uv sync --frozen` fails inside the image build.

```bash
curl -LsSf https://astral.sh/uv/0.12.5/install.sh | sh   # match backend/Dockerfile
export PATH="$HOME/.local/bin:$PATH"                     # or reopen the shell
uv --version

cd backend
uv sync                                    # creates .venv from uv.lock
uv run python -c "import app.core.config"  # sanity check
```

`uv` also downloads Python 3.12 itself (`uv python install 3.12`) — no system Python work.

### 2.2 Node 24 + npm — pinned to `frontend/.nvmrc`

Ubuntu/Debian archives often ship an older Node than this repo accepts (`engines` requires
`node >=24.19.0`, `npm >=11.17.0`). Use a version manager so `.nvmrc` is authoritative:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"

cd frontend
nvm install          # reads .nvmrc → 24.19.0, and switches to it
nvm use
node -v && npm -v    # v24.19.0 / 11.x

npm ci               # lockfile-exact install (never `npm install` for this)
```

### 2.3 Pre-commit hooks

```bash
cd backend
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
```

The hooks run `ruff`, `detect-secrets`, `commitizen` and the local frontend hooks. The frontend
ones shell out to `npx eslint` / `npx prettier`, so **`node` must be on the PATH of the shell that
runs `git commit`**. If `nvm` is only loaded in interactive shells, a hook run from a task or IDE
fails with `npx: command not found`; run commits from a terminal where `nvm use` has been applied.

### 2.4 The full gate, once

```bash
make lint && make types && make test        # backend + frontend, all mocked, no API keys
cd frontend && npm run test && npm run build
```

---

## 3. Repository and registry

```bash
git clone https://github.com/<owner>/taylored_personal_assistant.git
cd taylored_personal_assistant
git checkout v<tag>        # or stay on main; the Makefile derives APP_VERSION from
                           # backend/app/_version.py, which release-please keeps at the latest release
```

The GHCR images are **private**. Log in with a token that carries **`read:packages`** (a classic
PAT scoped to nothing else is enough; fine-grained tokens need *Packages: read*):

```bash
docker login ghcr.io -u <owner>
```

Verify the release exists before starting anything:

```bash
docker pull ghcr.io/<owner>/taylored-personal-assistant-backend:<tag>
docker pull ghcr.io/<owner>/taylored-personal-assistant-frontend:<tag>
```

---

## 4. The two secret files

Both are read at container creation and both are git-ignored (`.gitignore` covers `.env.*` and
re-includes only `*.example`). Create them from their templates and **lock them down**:

```bash
cp infra/compose/.env.prod.example infra/compose/.env.prod
cp backend/.env.prod.example       backend/.env.prod
chmod 600 infra/compose/.env.prod backend/.env.prod
```

Compose's `.env` parser treats `#` as a comment **only at line start** — keep comments on their
own lines, and never leave a trailing comment after a value.

### 4.1 `infra/compose/.env.prod` — stack secrets

| Variable | How to produce it | Constraint |
|---|---|---|
| `ENCRYPTION_KEY` | `openssl rand -hex 32` | **exactly 64 hex chars**; Langfuse refuses to start otherwise |
| `SALT` | `openssl rand -hex 32` | — |
| `NEXTAUTH_SECRET` | `openssl rand -hex 32` | — |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` | Langfuse's own Postgres |
| `REDIS_AUTH` | `openssl rand -hex 24` | — |
| `CLICKHOUSE_PASSWORD` | `openssl rand -hex 24` | — |
| `MINIO_ROOT_PASSWORD` | `openssl rand -hex 24` | also feeds the S3 upload keys |
| `APP_DB_USER` / `APP_DB_NAME` | `assistant` (default) | — |
| `APP_DB_PASSWORD` | `openssl rand -hex 24` | **alphanumeric only** — it is inlined into `CHECKPOINT_DB_URL`; `:`, `@` or `/` break the URL |
| `LANGFUSE_INIT_PROJECT_PUBLIC_KEY` | `pk-lf-$(openssl rand -hex 12)` | must equal the backend's `LANGFUSE_PUBLIC_KEY` |
| `LANGFUSE_INIT_PROJECT_SECRET_KEY` | `sk-lf-$(openssl rand -hex 24)` | must equal the backend's `LANGFUSE_SECRET_KEY` |
| `LANGFUSE_INIT_USER_PASSWORD` | a strong password | first Langfuse login |
| `NEXTAUTH_URL` | `http://<host>:3000` | **exactly** what you type in the browser; a name that does not resolve breaks the login callback |

All seven random secrets must be **distinct** — do not reuse one value in several rows.

> **Headless init runs once.** These keys create the org, project and admin user on the *first*
> boot of an empty database. Changing them later does nothing (the project keeps its original
> keys) and the backend silently starts sending rejected credentials. To rotate afterwards, create
> a new key pair in the Langfuse UI and update `backend/.env.prod` only.

### 4.2 `backend/.env.prod` — runtime configuration

| Variable | Value / source | Notes |
|---|---|---|
| `ENV` | `prod` | the app requires `dev` or `prod` |
| `DEEPSEEK_API_KEY` | from DeepSeek | fail-fast: the app will not boot without it |
| `ASSISTANT_API_KEY` | **the same value as the `VITE_API_KEY` GitHub secret** | see §0; 32+ URL-safe chars |
| `DEFAULT_TIMEZONE` | e.g. `Europe/Berlin` | IANA name |
| `SUPPORTED_LANGUAGES` | `["en","de","pt-BR"]` | JSON list |
| `VOICE_ENABLED` | `true` to turn voice on | boot fails if a key or a voice is missing |
| `VOICE_STT_API_KEY` | Groq (`gsk_…`) or OpenAI | STT provider |
| `VOICE_TTS_API_KEY` | OpenAI (`sk-…`) | TTS provider |
| `VOICE_TTS_VOICES` | omit for the defaults (`en: marin`, `de: cedar`, `pt-BR: coral`) | every supported language needs a voice |
| `LANGFUSE_ENABLED` | `true` | best-effort: traces are dropped, the app keeps working |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | **the same pair as `LANGFUSE_INIT_PROJECT_*`** | see §0 |
| `LANGFUSE_BASE_URL` | `http://localhost:3000` is fine | the prod overlay overrides it to the compose service name |

Do **not** set `CHECKPOINT_DB_PATH` in prod — `docker-compose.prod.yml` injects
`CHECKPOINT_DB_URL` (Postgres) and a stray SQLite path would only confuse the picture.

### 4.3 Recovering the baked frontend key

If you inherited a deployment and nobody remembers `VITE_API_KEY`, read it out of the published
image instead of guessing:

```bash
docker run --rm --entrypoint sh \
  ghcr.io/<owner>/taylored-personal-assistant-frontend:<tag> \
  -c 'grep -rhoE ".{0,120}X-API-Key.{0,10}" /usr/share/nginx/html/assets/*.js' \
  | grep -oE '[A-Za-z0-9_-]{40,}'
```

That prints the key the SPA sends (one 40+ character URL-safe literal per matching bundle; a
single-chunk build yields exactly one line). Then either copy it into `ASSISTANT_API_KEY`, or treat
it as compromised, set a new `VITE_API_KEY` secret and cut a new release.

---

## 5. Validate before starting

Cheap checks that catch the failures above while they are still free to fix:

```bash
# 1. The compose model resolves — needs APP_VERSION, which the Makefile supplies.
APP_VERSION=$(sed -n 's/.*__version__ = "\(.*\)".*/\1/p' backend/app/_version.py) \
docker compose -p taylored-assistant-prod \
  -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml \
  --env-file infra/compose/.env.prod --profile observability config -q

# 2. No placeholders left in either file
grep -rn "REPLACE_ME" infra/compose/.env.prod backend/.env.prod || echo "clean"

# 3. File modes
ls -l infra/compose/.env.prod backend/.env.prod        # -rw------- both

# 4. The provider keys are live (200 = accepted, 401 = wrong key)
set -a; . backend/.env.prod; set +a
curl -s -o /dev/null -w 'deepseek %{http_code}\n' -H "Authorization: Bearer $DEEPSEEK_API_KEY" https://api.deepseek.com/models
curl -s -o /dev/null -w 'groq     %{http_code}\n' -H "Authorization: Bearer $VOICE_STT_API_KEY" https://api.groq.com/openai/v1/models
curl -s -o /dev/null -w 'openai   %{http_code}\n' -H "Authorization: Bearer $VOICE_TTS_API_KEY" https://api.openai.com/v1/models
```

A Groq `403` with `error code: 1010` from a *script* is Cloudflare rejecting the client
fingerprint, not a bad key — retry with `curl` (as above) before regenerating anything.

---

## 6. First start

```bash
make prod-pull      # proves registry auth and that <tag> exists
make prod-up        # pull + up -d --no-build --wait
```

What to expect:

- The **first** boot is slow: Langfuse runs its Postgres **and** ClickHouse migrations. If
  `--wait` times out, watch `make prod-logs` before assuming a failure.
- Nine containers: `backend`, `frontend`, `app-db`, `postgres`, `redis`, `clickhouse`, `minio`,
  `langfuse-web`, `langfuse-worker`.
- `langfuse-worker` has **no healthcheck by design** and shows as merely `Up`; everything else
  should read `Up (healthy)`.

---

## 7. Verify the first run

| # | Check | Command / action |
|---|---|---|
| 1 | Every service healthy | `make prod-ps` |
| 2 | API alive | `curl -s http://<host>:8080/health` |
| 3 | UI loads and a **typed** turn answers | browse `http://<host>:8080` |
| 4 | **Voice** works | browse `http://localhost:8080` **on the host** and press **Speak** — the microphone needs a secure context, and a plain-HTTP LAN address is not one (see [`voice.md`](voice.md) → "Browser microphone") |
| 5 | Traces land | Langfuse at `http://<host>:3000` (first login: `LANGFUSE_INIT_USER_EMAIL`, default `admin@home.local`, password from `.env.prod`) shows an `assistant:<route>` trace |
| 6 | The two match rules really hold | `curl` a protected route with the key (200) and without (401); the trace in step 5 proves the Langfuse pair |

Then finish the appliance work from [`deploy.md`](deploy.md): backups (`infra/backup/`, including a
real restore drill) and the restart-survival proof. On a host with an encrypted root, "survives a
reboot" means *returns after the passphrase* — do not claim an unattended cold boot.

---

## 8. Troubleshooting the first run

| Symptom | Cause | Fix |
|---|---|---|
| `npm: command not found` | Node not installed, or the shell predates the nvm install | §2.2, then open a new shell |
| `npx: command not found` inside a git hook | The hook's shell has no nvm PATH | Commit from a terminal where `nvm use` ran |
| `uv: command not found` | `~/.local/bin` not on PATH, or run from a non-interactive shell | `export PATH="$HOME/.local/bin:$PATH"` |
| `permission denied ... /run/docker.sock` while `id` lists the group | Session predates `usermod -aG docker` | Log out and back in, or `newgrp docker` |
| `pull access denied` / `unauthorized` on GHCR | Not logged in, or the token lacks `read:packages` | §3 |
| **"Authentication failed — check VITE_API_KEY"** | `ASSISTANT_API_KEY` ≠ the `VITE_API_KEY` secret baked into the image | §4.3 to recover the baked value, then align the backend (§0) |
| Langfuse login redirects in a loop or fails | `NEXTAUTH_URL` ≠ the browser's exact address | §4.1 |
| Stack healthy, **Traces empty** | Langfuse key pair mismatch, or `LANGFUSE_ENABLED` false | §0 and §4.2 |
| `ENCRYPTION_KEY` invalid at startup | Not exactly 64 hex characters | `openssl rand -hex 32` |
| Backend restarts with a URL parse error | `APP_DB_PASSWORD` contains `:`, `@` or `/` | Alphanumeric only |
| `--wait` times out on first boot | Langfuse migrations still running | `make prod-logs`, wait, re-run `make prod-up` |
| **"The microphone needs a secure context"** / "Microphone access was refused" | Browsing a LAN IP over HTTP | Use `http://localhost:8080` on the host, or put the UI behind HTTPS |
