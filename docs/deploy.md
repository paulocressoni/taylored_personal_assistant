# Deployment runbook — the mini PC

The operator's entry point for getting this solution onto the mini PC and keeping it
updated **by hand**. CI builds, scans and publishes images; **nothing deploys itself.**
Automated CD was deliberately dropped, so the release you run is the release you pulled.

| | |
|---|---|
| Host | Mini PC on the home LAN, reachable at a **reserved** address |
| Transport | none — **LAN-only**, no VPN overlay and no forwarded ports. Administer it from inside the network |
| Published ports | **8080** — nginx: the UI *and* `WS /ws/voice` (the satellite connects here) · **3000** — Langfuse UI |
| Compose project | `taylored-assistant-prod` (device volumes/network are separate from `taylored-assistant-dev`) |
| Golden rule | the git tag IS the image tag IS the deployed version — **never deploy `:latest`** |

Because there is no overlay network, **`ufw` is the only boundary**: keep it to SSH plus the LAN
range on 8080, and nothing else.

---

## Where the details live

This page is the *flow*; the mechanics live next door and are not duplicated here.

| Need | Read |
|---|---|
| Docker/compose commands, image tags, GHCR auth | [`docker-cheatsheet.md`](docker-cheatsheet.md) → "Published images (GHCR)" |
| Langfuse stack upgrade, reboot survival, post-reboot checks | [`observability.md`](observability.md) → "Upgrading", "Reboot survival" |
| Backups, encryption, the monthly restore drill | [`../infra/backup/BACKUP.md`](../infra/backup/BACKUP.md) |
| Why the milestones are ordered this way | [`implementation_plan.md`](implementation_plan.md) → Delivery 1 |

---

## 1. First deploy (once)

1. **Get the code and log in to GHCR** on the box (images are private by default):
   `docker login ghcr.io -u <owner>`.
2. **Create the two secret files from their templates** — they are never committed:
   - `infra/compose/.env.prod` (mode 600): Langfuse secrets, `APP_DB_*`
     (**keep the password alphanumeric** — it is inlined into a URL), and `NEXTAUTH_URL`
     set to the box's real LAN name/IP.
   - `backend/.env.prod` (mode 600): `DEEPSEEK_API_KEY`, `ASSISTANT_API_KEY`, and the voice keys.
3. **Turn voice on.** Uncomment `VOICE_ENABLED=true` plus `VOICE_STT_API_KEY` and `VOICE_TTS_API_KEY`.
   The app **fails fast at startup** if the switch is on and a key is missing, or if any supported
   language has no TTS voice — a missing voice is a boot failure, not a silent degradation.
4. **Deploy:** `APP_VERSION=<tag> make prod-pull` then `make prod-up`. `prod-up` pulls first and
   passes `--no-build`, so prod never compiles the working tree.
5. **Verify** — run the checklist in §4. Do not skip the voice turn.
6. **Prove reboot survival.** `sudo systemctl enable --now docker`, confirm every service carries
   `restart: unless-stopped`, then **yank the power** (not a clean `poweroff`) and confirm the whole
   stack returns unattended. `unless-stopped` does *not* recover from `make prod-down`, which
   removes the containers.
7. **Turn on backups** and finish the host-specific items at the top of
   [`BACKUP.md`](../infra/backup/BACKUP.md) — the recorded verification pass ran on Windows/WSL2, so
   the `VOLUMES` tars, the cron line and a real drill are still owed **on the box that runs the
   daemon**. An untested backup is not a backup.

---

## 2. Routine update

1. **Read the release notes / `CHANGELOG.md`** for the tag you are about to run. The interesting
   lines are breaking changes and anything touching the checkpointer or the Compose files.
2. **Confirm last night's backup succeeded** (or take one now). This is the only cheap undo —
   especially before an observability-stack bump.
3. **Note the tag you are currently running**, so §3 is one command away.
4. **Deploy the new tag.**
5. **Verify** — §4.

## 3. Rollback

Images are immutable, so rolling back the **app** is a re-pull and an `up`:

```bash
# Raw compose, NOT `make` — see the trap below.
export APP_VERSION=0.3.1          # the previous, known-good tag
docker compose -p taylored-assistant-prod \
  -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml \
  --env-file infra/compose/.env.prod --profile observability \
  pull && docker compose -p taylored-assistant-prod \
  -f infra/compose/docker-compose.base.yml \
  -f infra/compose/docker-compose.prod.yml \
  --env-file infra/compose/.env.prod --profile observability \
  up -d --no-build --wait
```

> ⚠️ **Trap: `APP_VERSION=<tag> make prod-up` does not do what it looks like.**
> The `Makefile` *assigns* `APP_VERSION` itself (derived from `backend/app/_version.py`), and in GNU
> make a makefile assignment **overrides** an environment variable of the same name. So a prefix like
> `APP_VERSION=0.5.0 make prod-up` is silently ignored, and you deploy whatever `_version.py` says in
> the checked-out tree. That is usually harmless — `release-please` keeps `_version.py` equal to the
> latest release, so `make prod-up` on `main` deploys the latest release, which is the normal flow —
> but it means **the make targets cannot select an arbitrary tag.** Use either the raw compose
> commands above, or `git checkout v<tag>` before `make prod-up`.
> (`docker-compose.prod.yml`'s own usage block shows the raw form, where the variable *is* honoured,
> so the two are consistent with each other and only the `make` path differs.)

**Schema caveats — rollback is safe for the app, not for the observability stack:**
- The **app** adds no migrations of its own; conversations written by a newer app version remain
  readable by an older one unless a release explicitly bumps the checkpointer schema.
- **Langfuse** migrates its Postgres **and** ClickHouse forward when `langfuse-web` starts, and
  **ClickHouse downgrades are not supported.** Rolling the observability stack back is therefore
  not a supported undo — restore from a dump instead. See [`observability.md`](observability.md).

## 4. Verify after any deploy

1. `make prod-ps` — every service `Up (healthy)`, except `langfuse-worker`, which has no healthcheck
   by design and shows as merely `Up`.
2. `http://<host>:8080/health` answers, and the UI loads.
3. **A typed chat turn** returns an answer.
4. **A voice turn** — press **Speak** in the composer, or drive the socket directly from a dev machine:
   `uv run python scripts/smoke_09_voice_ws.py --url ws://<host>:8080/ws/voice`.
   Note the backend publishes **no host port** in prod, so the socket is only reachable through nginx
   on 8080 — and the script takes the API key from your *local* `backend/.env.dev`, which must match
   the box's `ASSISTANT_API_KEY`. This is the check that proves the whole audio path including the
   nginx `/ws` upgrade, and it is the one people skip.
5. Langfuse shows a new `assistant:<route>` trace for that turn — one check covering backend →
   ingest → ClickHouse end to end.
6. From a **different** machine on the LAN: 8080 answers (`0.0.0.0`-bound, as intended) and nothing
   else unexpected is reachable.

---

## 5. Two traps that will cost you an evening

**The frontend's API key is baked in at build time.** The SPA must present the backend's
`ASSISTANT_API_KEY`, but Vite inlines `VITE_*` only at build time and the frontend's `.dockerignore`
keeps `.env*` out of the build context. The publish workflow injects it from the **`VITE_API_KEY`
GitHub repository secret**, which must equal the backend's `ASSISTANT_API_KEY`. So **changing the
shared key requires all three**: update the secret, cut a new release, re-pull the frontend image.
Changing only the backend's value leaves the SPA unable to authenticate, with a 401 that looks like
a backend problem.

**The satellite has the server address and key compiled into its firmware.** If the box's address
changes — a DHCP lease moving, for instance — the satellite stops working *silently*, and the
symptom looks exactly like a firmware or Wi-Fi bug. This is why the address is reserved (see the
plan, D1.1 check 1). If a satellite ever goes quiet, verify the box's address **before** touching
the firmware.
