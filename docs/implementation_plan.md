# INTELLIGENT PERSONAL ASSISTANT (IPA) — IMPLEMENTATION PLAN v7

Updated 2026-10-23. **This is the single plan of record.** It supersedes the original v6 plan, which has been
retired — where this document and any older plan disagree, this one wins.

**Target host (prod):** **one** 64-bit Linux machine on the home LAN that meets §3 — any current distro, server or
desktop edition, with Docker Engine + Compose v2. The plan deliberately names no model: the stack needs a stable
address, a container runtime enabled at boot, a synced clock and enough disk, and nothing else.
**Stack:** LangGraph 1.2 + DeepSeek V4.1 | FastAPI | React/Vite/TS | Docker Compose | self-hosted Langfuse.
**Languages:** English, German, Portuguese (pt-BR) — first-class in every milestone.

**How to read this document.** All paths are relative to the **repository root**. Each milestone is written to be
self-contained: paste one `###` block into a code-agent session, tell it to follow the steps in order, and confirm
the `DONE WHEN` before moving on. Sections §1–§3 are orientation (where the project actually is, what has changed
in the ecosystem, and what a deployment host must provide); §4 is the current priority; §5 onward is the backlog.

---

## 0. How v7 differs from v6

| v6 said | v7 says | Why |
|---|---|---|
| Assemble an ASRock DeskMini X600 + Ryzen 7 8700G (M24) | Shrink to a **pre-flight check** on a machine you already own | No assembly; the host only has to meet §3 |
| Self-host an LLM on the Radeon 780M iGPU (M34) | **Dropped** | No deployment host in this class has a usable GPU compute path |
| Automated CD from CI, behind an approval gate (M27) | **Dropped** → replaced by a manual release/update runbook | You pull the latest release onto the host by hand |
| A VPN overlay for remote access (v6's M25/M26/M27 all assumed one) | **Dropped** | Not using one. The box is **administered on the LAN**; there is no remote-administration path to design or secure |
| "A fully working product at M12, before hardware" | Hardware exists and the voice pipeline is built; the product is **one satellite away from being usable** | Reality of the tree |
| Voice is Phase 7, after deploy and Langfuse | Voice is **the product**; text UI is the escape hatch | The satellite is how you actually use this |
| Chroma vector store (M20) | **pgvector** in the existing Postgres | One datastore, actively maintained; Chroma's release cadence stalled |
| APScheduler AsyncIOScheduler + SQLAlchemy jobstore (M17) | **APScheduler 3.11.3 is the only stable line** (4.x is alpha since 2025-04); own `scheduled_items` table is the source of truth and the scheduler is a thin poller | Durability must not depend on a library's jobstore schema |
| Wake word = on-device WakeNet, whole custom firmware (M33, "optional, advanced") | **Server-side wake word, thin I2S firmware** | Fits the server-owns-endpointing design already shipped; firmware stays replaceable |

Everything already built is listed in §1 and is **not** to be rebuilt. Everything still to do is in §4–§7.

---

## 1. Where the project actually is (verified against the tree, 2026-10-14)

Release: **v0.3.1** (`backend/app/_version.py`), plus four manual `v0.4.x-test` tags used to exercise the publish pipeline.

### 1.1 Done — do not rebuild

| # | Milestone | Evidence |
|---|---|---|
| M00 | Monorepo, dual-env config, tooling | `backend/app/core/config.py`, `Makefile`, `pyproject.toml` |
| M01 | Trunk-based workflow, SemVer, pre-commit | `CONTRIBUTING.md`, `.pre-commit-config.yaml`, `v0.1.0` |
| M02 | LLM provider layer (DeepSeek) + smoke tests | `backend/app/core/llm.py`, `backend/scripts/smoke_01..05` |
| M03–M05 | LangGraph agent, router + ReAct tool loop, trilingual core | `backend/app/graph/*`, `backend/app/language/detector.py` |
| M06 | Mocked unit tests + CI | `backend/tests/` (27 modules), `.github/workflows/ci.yml` |
| M07 | Containerized backend (multi-stage) | `backend/Dockerfile` |
| M08 | Self-hosted Langfuse (dev) | `infra/compose/docker-compose.base.yml` (`observability` profile) |
| M09 | FastAPI + WS chat streaming, shared-key auth, rate limit | `backend/app/api/*` |
| M10 | React chat UI, token streaming, mic + voice client | `frontend/src/` |
| M11 | Conversation checkpointing (SQLite dev) + multi-session UI | `backend/app/graph/checkpointer.py` |
| M12 | Full local dev stack (Compose) | `infra/compose/docker-compose.{base,dev}.yml` |
| M22 | Release automation (release-please) | `.github/workflows/release-please.yml` |
| M23 | Image build + Trivy gate + SBOM + GHCR publish | `.github/workflows/docker-publish.yml` |
| M28 | Langfuse in production: stack, dashboards, alerts, encrypted backups | `infra/compose/docker-compose.prod.yml`, `infra/backup/`, `infra/observability/` — **not yet run on the real box** |
| M29 | Multilingual voice pipeline (server-side VAD, STT, streamed TTS, barge-in, per-stage timing) | `backend/app/voice/*`, `WS /ws/voice`, `frontend/src/hooks/useVoiceSession.ts` |

### 1.2 Not implemented — the actual backlog

M13, M14, M15, M16, M17, M18, M19, M20, M21, M30, M32, M33 — plus everything in §6.

The only scaffolding that exists:
- `backend/app/identity/` and `backend/app/memory/` are **docstring-only packages**.
- `IPAState` already carries `principal`, `pending_action`, `permission_denials`, `device_id` (unused today).
- `backend/app/tools/registry.py` registers exactly **one** tool (`calculate`).
- Frontend has `lib/deviceId.ts` (used) and `hooks/useAlarmSound.ts` (**unwired scaffold, no caller**).
- `firmware/` contains **only `.gitignore`-style `.gitkeep`** — no firmware exists.

### 1.3 Voice pipeline: what is actually true today

- `WS /ws/voice` is **long-lived**, many turns per socket: 16 kHz mono s16le PCM in; PCM + control frames out.
- **The server owns endpointing** (vendored Silero VAD v6.2.3 ONNX via `onnxruntime`, no torch). The client runs **no** VAD. Barge-in works; an interrupted turn is persisted as `[interrupted]`.
- STT = Groq `whisper-large-v3-turbo` ($0.04/hr). TTS = OpenAI `gpt-4o-mini-tts`, one voice per language
  (`en: marin`, `de: cedar`, `pt-BR: coral`), synthesised **per sentence while the graph is still generating**.
- Voice turns land in the **same thread** as typed ones.
- `device_id` is already carried end-to-end (chat + voice) — but nothing maps a device to a connection.
- **Measured latency (5 turns, fixture):** `first_audio_ms` p50 **≈3.16 s**, `perceived_ms` p50 **≈3.46 s**.
  Dominated by `graph_ttft_ms` p50 **≈2.23 s**; TTS round trip ≈0.6 s; STT ≈0.27 s.
  **The v6 target of "<1.5 s" is not met and is not reachable without a voice fast path** — `docs/voice.md`
  says this explicitly. Treat it as a decision to take, not a bug.
- Known gaps a satellite will hit: **no wake word**, no diarization, plain `ws://` (no TLS), no client-side queue
  if the socket drops, audible TTS chunk seams, and the socket **never reports the detected language**.

### 1.4 Deployment reality

- `docker-compose.prod.yml` pulls immutable `ghcr.io/...:{APP_VERSION}` images, adds `app-db` (Postgres 17.11) and
  moves the checkpointer to Postgres. **Prod publishes only `8080` (nginx) and `3000` (Langfuse).**
- `frontend/nginx.conf` proxies **`/ws` → `backend:8000`** (and `/health`, `/sessions`, `/openapi.json`), with
  `proxy_read_timeout 3600s`. **A satellite therefore reaches the voice socket at `ws://<host>:8080/ws/voice`** —
  no new port has to be exposed.
- Backend idle timeout is **60 s** (`voice_idle_timeout_seconds`); a quiet device must keepalive (≈20 s) or be dropped.
- `voice_max_sessions = 3`.
- **No workflow deploys to a host.** CI builds/scans/publishes only. `infra/backup/BACKUP.md` still carries a
  "**TODO (before the host goes live)**" — the M28 verification pass ran on a laptop, not on a deployment host.

---

## 2. What changed in the world (researched and verified 2026-10-14)

Sources are cited per item; treat unverified rows as open questions, not facts.

### 2.1 LLM — DeepSeek

- Current model IDs are **`deepseek-flash`** (= DeepSeek-V4.1-Flash, 2026-09-10) and **`deepseek-v4-pro`**.
  **`deepseek-chat` and `deepseek-reasoner` were retired 2026-07-24.** `deepseek-v4-flash` is itself retired but
  still silently routed to V4.1-Flash.
  **→ the repo's default `deepseek_model_flash = "deepseek-v4-flash"` is a dead name that works by accident.**
- 1 M context, 384 K max output, tool calling ✓, JSON ✓, **vision ✓ on flash**, concurrency 2500 (flash) / 500 (pro).
- **Peak/off-peak pricing** (peak = 01:00–04:00 and 06:00–10:00 UTC, Mon–Fri): flash cache-miss input
  $0.15/$0.30, output $0.60/$1.20 per 1 M. **Cache-hit input is ~2 % of cache-miss** — the prompt-prefix cache is
  the single biggest lever on both cost and latency.
- Thinking is **on by default** with `reasoning_effort` low/high/max; `llm.py` already sends an explicit
  `False`, which is the right call for the router/knowledge/responder roles.

### 2.2 Orchestration

- **LangGraph 1.2.15**, langgraph-checkpoint 4.3.0, checkpoint-postgres 3.1.3, checkpoint-sqlite 3.1.2.
  The repo is on `langgraph>=1.2.11` — current. v1 removed `create_react_agent`/`ValidationNode`; `StateGraph`,
  checkpointers, `interrupt()` and `Command` are unchanged. **New in 1.2: event streaming (`astream_events(version="v3")`).**
- **langchain 1.4.5** / `langchain-core` 1.6.10; `langchain-deepseek` **1.1.1** (docs for it are stale — they still
  show the retired model names).
- There is **no official FastAPI/WebSocket streaming recipe**; the documented path is "use `astream_events(v3)` and
  wire your own transport" (or the LangGraph SDK `useStream` hook in React). The repo's hand-written WS layer stays.

### 2.3 Speech

- **STT:** Groq `whisper-large-v3-turbo` is still **$0.04/hr** — keep it. If streaming STT is ever wanted,
  **AssemblyAI Universal-Streaming Multilingual ($0.15/hr)** explicitly covers **en/es/pt/de/fr/it**.
- **TTS:** OpenAI's own docs say `gpt-4o-mini-tts` voices are **"currently optimized for English"** — which matches
  the honest note in `docs/voice.md`. Multilingual-first alternatives in 2026:
  **ElevenLabs v4 Turbo** ($0.04/1k chars, ~100 ms TTFB, 90+ languages) and **Cartesia Sonic 3.6** (native de + pt).
  **Deepgram Aura-2 is disqualified — it has no Portuguese.**
- **Realtime speech-to-speech exists** (OpenAI `gpt-realtime-2.1`, audio output $64/1M tokens ≈ $0.03–0.06/min;
  Gemini Live ≈$0.005/min in / $0.018/min out) but costs materially more than the chained pipeline and its
  de/pt-BR quality is unverified. **Do not adopt yet** — revisit only if latency work stalls.

### 2.4 Voice hardware — this is the material change

- Seeed now ships **three** XVF3800 products: **USB 4-mic array (no XIAO)**, **USB 4-mic array + XIAO ESP32-S3**
  (ships with I2S firmware), and **Flex**. *(You have the middle one: 4-mic array, case, pre-soldered XIAO.)*
- **Firmware variants and current versions:** **USB v2.1.1** and **I2S v1.0.9**
  (`github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY` → `xmos_firmwares/`).
  USB images are **16 kHz/6-channel** or **48 kHz/2-channel**; I2S images are **48 kHz master** or **16 kHz slave**,
  2-channel 32-bit. **There is no "16 kHz stereo 32-bit over USB" profile.** *(The repo's `docs/voice.md` already
  documents the I2S 16 kHz/stereo/32-bit contract correctly — keep that and ignore the USB profiles.)*
- **USB capture bug (only affects a USB bring-up):** capture sounds robotic / ~1.5× fast unless a playback stream is
  also open; community root-cause + workaround in `rvsh2/xvf3800-capture-fix`. **The I2S path avoids this entirely**
  — one more reason the satellite is I2S, not USB.
- **Playback must use the DSP path, not the DAC bypass** (`I2S_DAC_DSP_ENABLE=1`,
  `AUDIO_MGR_FAR_END_DSP_ENABLE=1`, tuned `AUDIO_MGR_SYS_DELAY`) so the XVF3800 has a far-end reference for AEC.
  Otherwise **barge-in and server VAD both hear the assistant's own voice**.
- **Seeed's XIAO getting-started wiki now exists** (pinout + GPIO table): `wn`-style pin mapping is still
  image-based; the best *textual* pin map is in `openensemble/voice-device-firmware` → `BUILD.md`.
  ⚠️ That repo is **AGPL-3.0** — read it for the pin map, do **not** copy code into this repo.
- **Real satellite firmwares now exist** (so this is no longer "write it from scratch"):
  `formatBCE/Respeaker-XVF3800-ESPHome-integration` (ESPHome ≥2026.9.0, patched `i2s_audio`, 106★, active) and
  `openensemble/voice-device-firmware` (ESP-IDF v5.4, wake word + AirPlay + OTA, AGPL-3.0).
- **Wake words are now multilingual on-device:** ESP-SR **2.6.0** / **WakeNet10** (2026-08-17) with released models
  `wn10_de_halloesp` ("Hallo ESP"), `wn10_pt_olaesp` ("Olá ESP"), `wn10_hiesp`, `wn10_en_heybuddy`, `wn10_heynova`,
  `wn9_jarvis_tts`. Also note **microWakeWord moved to the Open Home Foundation**
  (`OHF-Voice/micro-wake-word`, Apache-2.0) and **openWakeWord's last release was 2024-02-11 — stale.**
  This is why v6's "wake word is entirely the ESP32's job, expect to derive everything yourself" is no longer true —
  **but we are choosing server-side wake word anyway** (§4, D1.4).
- Ubuntu **26.04 ("Resolute Raccoon")** released 2026-04-23, GA **kernel 7.0**, **26.04.1** point release exists,
  **Docker CE officially supports it**, ROCm 10.1 supports it (irrelevant here — no GPU compute).
  ALSA/PipeWire handle UAC2 natively; **no 26.04-specific regression notes for this board were found** — treat
  "works out of the box" as unverified until D1.3 proves it on your hardware.

### 2.5 Everything else

- **Embeddings:** OpenAI `text-embedding-3-small` (1536d, $0.02/1M) is the cheap default;
  **Voyage `voyage-4`** (1024d, 32 k context, $0.06/1M) markets itself on multilingual quality.
  No independent de/pt-BR head-to-head was found — **calibrate on your own text** (§5, M20).
- **Vector store:** **pgvector 0.8.7 inside the Postgres you already run** is the least-friction choice for one node.
  Qdrant 1.19.2 if a dedicated service is ever wanted. **Avoid Chroma** (1.5.9, no release in ~5 months).
- **Scheduler:** **APScheduler 3.11.3** is the only stable line; **4.0 has been alpha since 2025-04-27**.
  Recommendation: own `scheduled_items` table as the source of truth + a thin asyncio poller.
- **Python:** 3.12 and 3.13 are both **security-only** now (3.13 since 2026-10-07); **3.14** is a year old and has
  cp314 wheels for every compiled dependency this project uses. The repo pins `>=3.12,<3.14` — i.e.
  **it excludes the current stable Python entirely.**
- **Frontend:** the repo is current (React 19.2, Vite 8.2, TS 5.9, Tailwind 4.3, ESLint 10). TS 7.0 exists; no rush.
- **Eval tooling:** Langfuse SDK 5.0.1 (already self-hosted here) + **promptfoo 0.124.1** (npm) for CI prompt
  regression. Skip DeepEval/Ragas until RAG metrics are actually needed.
- **Langfuse self-host needs Postgres + ClickHouse + Redis + MinIO.** That fits within the RAM in §3, but it is the
  largest memory consumer in the stack — budget for it.

---

## 3. Host requirements

**Status: requirements, not a shopping list.** The project targets **one** self-hosted Linux machine on the home
LAN. Nothing below names a model — each row states what the stack needs and how to check it on whatever machine you
use. A host that meets every row runs Delivery 1 unchanged.

| Area | Requirement | Why it matters | How to check it |
|---|---|---|---|
| **OS / runtime** | 64-bit Linux with Docker Engine ≥ 24 and Compose v2, **enabled at boot** | Every service is a container, and the stack must return after a reboot without anyone starting it | `docker --version`, `docker compose version`, `systemctl is-enabled docker` |
| **Edition** | Server or desktop edition both work; nothing assumes a headless install | The host may also be an everyday machine. The only desktop-specific requirement is disabling idle suspend (see **Power**) | — |
| **CPU** | 4+ cores, x86-64 or arm64 with published image support; **no GPU needed** | The workload is I/O-bound: the LLM, STT, TTS and embeddings are all remote APIs. There is no local inference in this plan | `nproc`, `uname -m` |
| **RAM** | 16 GB minimum, 32 GB comfortable | App Postgres plus Langfuse's Postgres/ClickHouse/Redis/MinIO, in one machine; ClickHouse is the largest consumer | `free -h` |
| **Storage** | ≥ 100 GB free on the filesystem backing `/var/lib/docker`, SSD strongly preferred | Images, conversation checkpoints and ClickHouse all grow; a spinning disk makes deploys and queries visibly slower | `df -h /var/lib/docker` |
| **Redundancy** | **None assumed** — one disk is acceptable, but then **backups must leave the machine** | A single-disk host holds no copy of its own data; the backup target you configure must not be that same disk | `infra/backup/BACKUP.md` |
| **Network** | A **stable address** (static or DHCP reservation) reachable from other LAN devices; wired preferred, Wi-Fi acceptable with power-save disabled | The satellite firmware compiles the server address in, and `NEXTAUTH_URL` must match the browser's address. A lease move looks exactly like a firmware bug | `ip -4 addr`, then confirm the pin in the router or `netplan`/`nmcli` |
| **Clock** | Timezone set, NTP synced, RTC in UTC | Alarms, timers and every checkpoint timestamp depend on it, and a wrong clock raises no error | `timedatectl` |
| **Power** | Idle suspend disabled; the PSU/adapter sized for sustained load | A suspended host looks exactly like a crashed stack: no HTTP, no socket, containers "gone" | GNOME hosts: `gsettings get org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type` |
| **Boot without help** | Optional, and only if you want unattended recovery: firmware set to power on after loss | With an **encrypted root** the machine stops at the passphrase prompt first, so unattended recovery is not achievable there — decide which of the two you want | Firmware setup, then a real power-cut test |

**Notes that keep the above honest:**

- **An encrypted root is supported**, but it changes reboot semantics: containers return only after someone unlocks
  the volume. D1.2's reboot criterion is written around that, and no doc should promise an unattended return on such
  a host.
- **The stack is CPU-light by design.** Everything expensive is a remote API. Meeting the minimums is enough to
  work; a bigger CPU buys latency headroom, not capability — and it is what **L1**'s latency numbers are measured on.
- **Only the published ports matter** (§1.4). The LAN is the only boundary: there is no VPN overlay (§8).
- **Record your own host's facts outside this repository.** OS version, CPU, RAM, disk layout, address reservation
  and SMART baseline are environment facts of one machine; committing them here would make a portable plan look
  hardware-specific. Keep them in a local runbook or deployment notes instead.

---

## 4. DELIVERY 1 — the satellite appliance (do this first)

**Goal:** standing in the room, say the wake word, ask a question, and hear the answer come out of the satellite's
speaker — wireless, surviving reboots, with LED feedback. Nothing else in this plan starts until this works.

Read `docs/voice.md` before D1.3. It is the authoritative description of the socket contract you are implementing against.

### D1.1 — Host pre-flight: verify what everything else depends on
*Replaces v6's M24 + M25. A **verification pass, not a build**: it only escalates if a check fails.*

**GOAL:** Confirm the handful of host properties that every later milestone silently assumes, so a failure here
surfaces as a failed check now rather than as an unexplained symptom three milestones later.

**WHY THIS IS SHORT:** the expensive, one-way work (provisioning the machine, firmware setup) is behind you. What
cannot be "already done" is anything that depends on *this deployment*: the address the satellite will be pointed
at, the container runtime, the clock alarms will be scheduled against, and evidence of how the machine behaves after
losing power. Those four are why this milestone exists. Everything else is a two-line command.

**CHECKS:**

| # | Check | How to check it | Why it matters — what breaks downstream | If it fails |
|---|---|---|---|---|
| 1 | **The address is fixed** (static IP or DHCP reservation) | Read the box's current address, then confirm it is pinned in the router's reservation table or in `netplan`/`nmcli` | ⚠️ **The one to do before D1.3.** The satellite's firmware has the server address compiled into it; a lease change breaks it silently, and you will debug firmware, wake word and Wi-Fi before suspecting the router. Also the value `NEXTAUTH_URL` and the future device registry point at | Add a DHCP reservation — five minutes |
| 2 | **Docker Engine + Compose, enabled at boot** | `docker --version`; `docker compose version`; `systemctl is-enabled docker`; `docker run --rm hello-world` | **Hard prerequisite** — nothing deploys without it. Boot-enabled matters because the entire stack is Compose and must come back after a restart; how far it can be automatic depends on check 6 | Install from Docker's official repo, then `systemctl enable --now docker` |
| 3 | **Free disk space, and where volumes will land** | `df -h /var/lib/docker`; `lsblk` | Every Docker volume, the Postgres data (checkpoints, scheduled items, later the vector store) and Langfuse's four datastores share **one** filesystem — assume there is no second drive to fall back on | Free space, or size the volumes deliberately so you know your headroom |
| 4 | **Clock: correct timezone, NTP synced, RTC survives a power cut** | `timedatectl` (want "System clock synchronized: yes"), set the zone if needed; then **after** the power-cut test below, check `date` again | M17 sets alarms and timers, M14 answers "what time is it", and every checkpoint is timestamped. A wrong clock throws no error — it silently fires your 07:00 alarm at the wrong moment. ⚠️ On older hardware the RTC coin cell is a wear item: if the clock jumps to a fixed past date after a power loss, the cell is dead | Set the timezone/NTP; replace the coin cell |
| 5 | **SSH: key-based, reachable from another machine on the LAN** | From your admin machine: `ssh <user>@<box>`; on the box: `sudo sshd -T \| grep -E 'passwordauthentication\|permitrootlogin'` | This is the **only** way you administer the box — there is no VPN overlay (see §8) — and it is how the D1.2 deployment is driven | Install your public key, then `PasswordAuthentication no` and `PermitRootLogin no` |
| 6 | **Power-loss behaviour matches your intent** | ❌ **Not checkable from inside the OS.** The only real test is physical: **yank the power cord** (not a clean `poweroff`) and see what happens | This is the single setting whose omission is **invisible until it matters**: M17's alarms, M15's pushes and the nightly backup all assume the stack is up. ⚠️ On a host with an **encrypted root** the machine stops at the unlock prompt, so the physical test proves only the firmware setting — decide whether unattended recovery is even a goal | Firmware → power-loss behaviour → the value you chose; record it |
| 7 | **Firewall: know what a LAN device can reach** | From another machine, probe the host's `8080` and `3000`; on the host, `sudo ufw status verbose` | With no overlay, the **LAN boundary is the only boundary**. Nothing should be listening on those ports yet, so this check is about knowing your starting state and confirming a firewall exists before the app publishes them | A firewall allowing **SSH + the LAN range on 8080** and nothing else |
| 8 | **The CPU does not throttle under sustained load** | Read the **wattage printed on the adapter/PSU**; watch temperatures and clocks during the D1.2 image pull/build | A power supply that is too small throttles under exactly the sustained load this workload creates (ClickHouse + Postgres + the always-on wake detector). That would corrupt the very latency numbers **L1** is measured against — you would be tuning against a thermally-limited host and never know | Size the adapter for sustained load, or accept the ceiling and write it down |

**DONE WHEN:** all eight check out, **or** the failures are ones you have explicitly accepted and written down with
the reason. Two are never optional: **the address fixed (1)** and **the container runtime working (2)** — their
absence produces silent, misattributed failures later. Check 6 is mandatory only if you want the stack to return
without a human present.

**STATUS — 2026-10-23, partially satisfied.** The pre-flight was run against the machine currently in use, which is
also a daily-use desktop; only the invariants that do not interfere with that were applied. The rest stay open and
are re-run when a host is dedicated to the solution — until then this milestone is **satisfied for a shared host**,
not complete.

| Check | State |
|---|---|
| 1 Address fixed | **done** — DHCP reservation, stable across renewals |
| 2 Docker + Compose, boot-enabled | **done** |
| 3 Disk headroom | **done** — verified, all volumes land on the same filesystem |
| 4 Clock | **verified, no change needed** |
| 5 SSH key-only | **deferred** — the operator is at the machine; convenience only |
| 6 Power-loss behaviour | **deferred** — needs a dedicated host, since an encrypted root makes the physical test inconclusive |
| 7 Firewall policy | **deferred** — firewall installed but inactive; nothing listens on 8080/3000 yet |
| 8 Thermal/power headroom | **not measured** — no sensor or SMART baseline on this host |

Two further changes were applied because D1.2 needs them and the eight checks do not cover them: the deploy toolchain
(`git`, `make`, `curl`) and desktop idle-suspend pinned to `nothing`. Neither is host-specific guidance.

**NOTES:** record the machine's facts while you are in there — OS version, CPU, RAM, disk layout, address
reservation and SMART baseline — **in your own deployment notes, not in this repository** (see §3). D1.2's backup
runbook and any future hardware conversation both want them.

**Notes carried over from the earlier, larger D1.1 analysis** (kept for traceability, no action needed now):
- **VT-x/VT-d is not required by Docker** — v6 claimed it was. Containers use namespaces and cgroups, not VMs.
  It is harmless if already enabled; do not go back into the firmware for it.
- **Single-channel RAM is a modest hit**, not the "roughly halves performance" v6 claimed — there is no local
  inference for an iGPU to accelerate.
- **Distro version is a support-lifetime question, not a compatibility one.** What matters is a current kernel and
  that the stack survives a reboot.

### D1.6 — Appliance operations hardening
*New in v7. Deliberately **not** on the delivery's critical path — nothing in Delivery 1 needs any of it.*

**GOAL:** Turn "a server that runs" into "a server I can operate, that patches itself, and that warns me before it
fails."

**STEPS:**
1. Confirm and write down the **remote-administration policy**: the host is **administered on the LAN only** — no
   VPN overlay. Nothing replaces that, and no `ufw` rule should be opened for one. If you ever change your mind,
   this is the step that changes; until then, "I administer it from inside the network" is the answer.
2. Enable **unattended-upgrades for security patches only, with no automatic reboot.** The nuance is the whole
   point: auto-rebooting an appliance at 03:00 is how you find out at breakfast that the stack did not come back.
3. Install **`lm-sensors`** and run `sensors-detect` (your board's Super I/O may need its own hwmon driver).
   Confirm real CPU temperature and fan RPM readings appear.
4. Add a small scheduled SMART + temperature check that alerts (the Langfuse alert webhook mechanism from M28 is
   already there to reuse, or `infra/observability/alerts.md`).
5. Record the host's facts in your local runbook — CPU model, PSU wattage, RAM configuration, disk serial and SMART
   baseline, address reservation — **not in this repository** (§3).
6. Decide the thermal policy in firmware (silent vs custom fan curve) once you know the real idle and load
   temperatures.

**DONE WHEN:** you can administer the host from inside the network, `sensors` reports real temperatures, an
unattended security update lands without any reboot, and a SMART/temperature alert actually reaches you.

### D1.2 — Deploy the stack to the host and actually verify it
*Replaces v6's M26 and closes M28's outstanding "before it goes live" TODO.*

**GOAL:** The v0.3.1 (or newer) release running from GHCR images on the deployment host, reachable on the LAN, with
working observability, working backups, and a **tested** restore.

**STEPS:**
1. On the host: clone the repo (deploy scripts + compose files only). Confirm the session that will run `make` sees
   the container group (`id -nG | grep -x docker` — group membership only applies to a **new** login), then
   `docker login ghcr.io`. The images are **private**, so this needs a token with `read:packages`.
2. Create `infra/compose/.env.prod` (mode 600) from `.env.prod.example`: Langfuse secrets, `APP_DB_*`
   (**password must stay alphanumeric** — it is inlined in a URL), and `NEXTAUTH_URL` pointing at the host's
   real LAN name/IP.
3. Create `backend/.env.prod` from its example, and **turn voice on for real**:
   `VOICE_ENABLED=true`, `VOICE_STT_API_KEY` (Groq), `VOICE_TTS_API_KEY` (OpenAI), and confirm
   `VOICE_TTS_VOICES` still covers en/de/pt-BR — the app refuses to boot otherwise.
   ⚠️ Set `ASSISTANT_API_KEY` to the **same value** as the `VITE_API_KEY` repository secret: the frontend bakes it
   in at build time, and a mismatch returns 401 on every request.
4. `APP_VERSION=<release> make prod-pull && make prod-up`. Deploy an **immutable** tag; never `:latest`.
5. Verify: `http://<box>:8080` serves the UI and `/health` answers; `http://<box>:3000` accepts a Langfuse login;
   a typed chat turn round-trips; **a browser voice turn round-trips** (`docs/voice.md` §Tooling lists the commands).
6. Make it survive reboots: a systemd unit (or `@reboot` cron) that runs `make prod-up`; confirm the compose
   services' `restart: unless-stopped` policy; **restart the host and prove the UI + voice both come back.**
   If the root volume is encrypted, "comes back" means after the unlock prompt — write that into the runbook
   rather than claiming an unattended return.
7. Install the backup cron from `infra/backup/` (Postgres dump of `app-db`, Langfuse's Postgres/ClickHouse/MinIO,
   the OAuth token slot, and the future `scheduled_items`), **encrypted**, and wire the Langfuse alert webhook.
8. **Run the restore drill on a throwaway project name** — restore the checkpoint DB and prove a conversation
   survives. `BACKUP.md` is explicit that an untested backup equals no backup. Then delete the scaffolding TODO.
9. Update `docs/observability.md` and `infra/backup/BACKUP.md` with the real hostname, paths and secrets layout.

**DONE WHEN:**
- The UI is reachable from another machine on the LAN, a chat turn and a voice turn both work, and Langfuse shows both traces.
- A full shutdown → start cycle brings the whole stack back **with no manual step beyond unlocking the root volume if
  it is encrypted**. Where the host is meant to boot unattended (no unlock prompt), prove that separately — including
  an **unclean** loss (the power-cord test from D1.1 check 6) once the stack is running.
- A restore from the nightly backup into a scratch project name produces a readable stored conversation.

**NOTES:** do not enable automated CD here — the update path is D1's runbook in §6 (M27). Keep `app-db` and
Langfuse's `postgres` separate; they have different backup scopes.

### D1.3 — Satellite firmware v1: wireless audio transport
*Replaces v6's M32/M33 for the first delivery. This is the heart of the delivery.*

**GOAL:** The XIAO ESP32-S3 + XVF3800 streams clean 16 kHz audio to `WS /ws/voice` over Wi-Fi and plays the
server's PCM answer through the XVF3800's speaker — with no wake word, no VAD and no beamforming logic in the
firmware, because the server already owns all of that.

**WHY THIS SHAPE:** the shipped contract has the **server** decide when a turn starts. Keeping the firmware to
"I2S in → 16-bit → frames → socket" and "socket → frames → I2S out" makes the satellite the dumbest possible
endpoint: no on-device DSP config to debug, no reflash to change behaviour, and the exact same server path the
browser already exercises.

**STEPS:**
1. **Flash the I2S firmware** onto the XVF3800 (I2C DFU only — the USB DFU path does not reach I2S images).
   Pick the **16 kHz profile** so the device and the server agree with **zero resampling**; the ESP32-S3 is the
   I2S master. Record the firmware version in the repo (`firmware/README.md`).
2. Configure the XVF3800 for the voice use case over I2C using the vendor control tool:
   **DSP-path playback** (`I2S_DAC_DSP_ENABLE=1`, `AUDIO_MGR_FAR_END_DSP_ENABLE=1`, tuned `AUDIO_MGR_SYS_DELAY`)
   — without this the mic hears the speaker and barge-in breaks; **`MUTE_FUNCTION_ENABLE=0`** so the mute button
   becomes a pure event (and re-send it after every boot, it is not persisted).
3. Write the firmware (ESP-IDF v5.4 recommended for full control and a clean OTA path later; ESPHome is an
   acceptable alternative if you would rather configure than code):
   - I2S read: **stereo 32-bit → take the left channel → downshift to 16-bit** (both I2S channels carry ASR beam 0).
   - Frame to **20 ms / 16 kHz mono s16le** (320 samples, 640 bytes) — the size the server expects from every
     other client.
   - Connect to `ws://<host>:8080/ws/voice?api_key=<key>`; do the nginx upgrade handshake.
   - Send the **start frame first**: `{"session_id": "<uuid>", "device_id": "<XVF3800 serial>", "output_sample_rate": 16000}`.
     The device declares its own serial as `device_id` — that is the identity the backend will use in M15.
   - Stream binary audio continuously. **Keepalive at least every 20 s** (the server drops a silent socket at 60 s).
   - Receive: binary frames → 16-bit → I2S out to the XVF3800 (which does the DSP + amp). Handle `ready`,
     `speech_start` (**flush the playout buffer — this is barge-in**), `transcript`, `token`, `audio_end`, `error`.
   - **Reconnect with backoff**; re-open the session on drop; never wedge.
   - LED ring (12 × WS2812): at minimum idle / listening / thinking / speaking / error.
   - Pin the audio I2S task and the Wi-Fi task to separate cores so network work cannot underrun the audio buffers.
   - Do **not** copy code from `openensemble/voice-device-firmware` — it is **AGPL-3.0**. Use it (and the Seeed
     pinout image) only to confirm the pin map.
4. **Bring-up sequence, in this order:** (a) prove capture on the bench (log levels, confirm 16 kHz, confirm the
   left channel is the ASR beam); (b) prove playback (stream a fixture WAV back through the speaker); (c) prove the
   socket against `backend/scripts/smoke_09_voice_ws.py` running on your laptop; (d) only then point it at the deployment host.
5. **Fallback if the audio path fights you:** temporarily flash the **USB** firmware and validate the whole pipeline
   tethered to the deployment host before continuing. ⚠️ If you do, the **known capture bug** applies — capture is
   robotic/1.5× fast unless a playback stream is kept open (`rvsh2/xvf3800-capture-fix`). The I2S path does not
   have this problem, which is why it is the target.
6. Commit a `firmware/README.md`: exact firmware versions, control-tool commands, pin map, build/flash procedure,
   and the LED meaning table.

**DONE WHEN:**
- Standing ~4 m away, speech goes out and the spoken answer comes back **from the satellite's speaker**, wirelessly.
- Saying something while the answer is playing **cuts the answer off** (the `speech_start` flush path works).
- Killing the Wi-Fi for 30 s and restoring it re-establishes the session with no power cycle.
- The satellite runs for 24 h unattended without wedging, dropping the socket permanently, or needing a reboot.

**NOTES:** `voice_max_sessions = 3` — a satellite holds one for as long as it is powered. Two satellites plus a
browser tab is already the cap; raise it deliberately, and remember each open session can hold a provider slot.

### D1.4 — Server-side wake word
*New in v7. Decision: the wake word lives on the server, not in firmware.*

**GOAL:** The satellite is not "always recording a conversation into the assistant". Idle room audio must not reach
STT or the graph until a wake phrase is heard — and changing the phrase must be a config change, not a reflash.

**WHY SERVER-SIDE:** the server already owns endpointing, so this is one more gate in front of a pipeline that
exists. It keeps the firmware of D1.3 unchanged and makes the wake word tunable with real recordings from the
actual room — which is the only data that matters for a keyword spotter.

**STEPS:**
1. **Choose the engine with a spike, not a spec sheet.** Candidates, all runnable on the host's CPU:
   Porcupine (commercial SDK, per-language models, licensed — check personal-use terms), Vosk keyword spotting
   (Apache-2.0, real de/pt models, grammar-constrained recogniser), and openWakeWord (widely used but
   **no release since 2024-02-11**). **Do not adopt openWakeWord without accepting that staleness.**
   ⚠️ **None of these was verified for German + Portuguese accuracy in this research.** Record ~20 real utterances
   from the satellite at 1 m and 4 m and measure false-accepts/false-rejects per engine per language. Pick on data.
2. Insert the gate in `backend/app/voice/session.py` **before** the endpointer's utterance reaches STT: audio is
   still consumed and VAD still runs (so `speech_start`/LED feedback stays honest), but a turn only enters the
   graph once the wake phrase is present in the pre-roll or the utterance window.
3. Keep the wake word **configurable and per-language** so the three supported languages each have a natural
   phrase. Add the settings (word, sensitivity, enabled) to `Settings` with `Field(description=...)`.
4. Instrument it: log/annotate wake detections and rejections so Langfuse can show the false-trigger rate.
   Include a config switch to run in **always-listening** mode for calibration sessions.
5. **Prove the negative case:** a TV/radio/podcast playing in the room must not open a turn.

**DONE WHEN:**
- Nothing is transcribed or sent to the graph unless the wake phrase was actually spoken.
- The phrase can be changed for all three languages by editing config and restarting — no reflash.
- Measured on your own recordings: false-accept and false-reject rates are known numbers, not guesses, at 1 m and 4 m.

**NOTES:** this deliberately spends CPU on the host for every idle second. Measure it — if the wake gate is
expensive, the *later* milestone is to move it to the XIAO (WakeNet10 now has released `wn10_de_halloesp` /
`wn10_pt_olaesp` models, and microWakeWord lives under the Open Home Foundation), keeping this server-side
detector as the reference implementation to test against.

### D1.5 — Delivery acceptance, latency on real hardware, and the runbook
*New in v7.*

**GOAL:** Know how it actually feels in the room, write down how to operate and repair it, and update the docs.

**STEPS:**
1. Measure with `backend/scripts/voice_latency.py` **through the satellite**, not on the fixture alone, and record
   `perceived_ms` — that is what you will actually feel. Expect **~3 s p50** as the baseline (§1.3).
2. Tune only the cheap knobs first, one at a time, re-measuring after each:
   `VOICE_VAD_MIN_SILENCE_MS` 500 → 300 is a straight **−200 ms of felt latency**; then the
   `VOICE_TTS_FIRST_CHUNK_*` bounds. Do **not** chase the last 50 ms.
3. Write `docs/satellite.md`: the firmware build/flash procedure, the XVF3800 control commands and their
   persistence behaviour, the LED meaning table, the wake-phrase config, the pairing/`device_id` story,
   and the recovery steps for "it stopped answering".
4. Update `README.md`'s roadmap table to point at this plan, and re-point the `docs/voice.md` "Known limitations"
   list at what is genuinely still open (`ws://` with no TLS, no client-side queue on socket loss, chunk seams,
   the language not being reported back to the device, de/pt-BR voice quality).
5. **Review `docs/deploy.md` while the deployment steps are fresh** and fill in the box's real hostname/IP
   (H3 already wrote the runbook; this step is where it gets the host-specific values and any correction from
   doing it for real).
6. **Decide, in writing, whether the current latency is acceptable.** If it is not, say so — that is what makes
   the latency fast path the next milestone instead of M15/M17.

**DONE WHEN:**
- `docs/satellite.md` exists and someone else could rebuild the satellite from it.
- The measured `perceived_ms` numbers are recorded in the repo next to the budget they are compared against.
- The README roadmap is true again.

**Fed back into the plan:** everything after this point is chosen with real usage data, as agreed. The ordering in
§5 is a **proposal** — re-rank it after a week of actually talking to this thing.

---

## 5. Proposed backlog after the delivery

Ordered by your stated preference (timers/alarms next) then by dependency. **Re-rank freely — this ordering is a
hypothesis until D1 is in daily use.**

### M15 — Device registry and server push channel
**Depends on:** D1 (a real device to push to) · **Original M15, re-scoped**

**GOAL:** The server can start a conversation with a device. Nothing today can: the model is request → response only.

**STEPS:**
1. `devices` table: `device_id`, `user_id`, `name`, `type` (`browser` | `satellite` | `cli`), `capabilities` (JSON),
   `trust_level`, `last_seen`. The satellite already sends its **XVF3800 serial** as `device_id` (D1.3) — use it.
2. **Per-device credentials, not the shared API key.** Today a satellite would carry the single assistant key in
   firmware; issue a per-device secret so a lost/replaced device can be revoked without rotating everything.
3. `WS /ws/device`: register, heartbeat, auto-reconnect with backoff. An in-memory `ConnectionRegistry` +
   `push_to_device()` is correct for **exactly one backend replica** — note in the code why Redis pub/sub is the
   answer if that ever changes (Redis already runs for Langfuse).
4. Offline fallback: try another online device of the same owner; if none, **queue and deliver on reconnect**.
   Prove it by killing the satellite's power, pushing, and powering it back on.
5. Frontend registers its device id on the same channel and handles `notification`, `alarm`, `confirm_request`.
   **Wire up the already-written `frontend/src/hooks/useAlarmSound.ts`** — it has been an unwired scaffold since M10.
6. Replace the satellite's socket-level alarm handling: alarms arrive on `/ws/device`, audio playback stays on
   `WS /ws/voice`. Keep the two concerns apart.

**DONE WHEN:** a debug `POST /debug/push` makes an open browser tab and a powered satellite both react, and a push
sent while the satellite is off is delivered when it comes back.

### M13 — User settings and profile store
**Depends on:** M11 · **Original M13**

**GOAL:** Exact, deterministic settings — deliberately **not** vector memory. A fuzzy similarity search must never
be able to return the wrong city's weather or the wrong alarm offset.

**STEPS:**
1. `user_settings` table: `user_id`, `timezone` (IANA), `home_lat`, `home_lon`, `home_label`,
   `preferred_language`, `units`, `alarm_sound`.
2. Tools `get_user_settings` / `update_user_setting` so "set my home to Munich" works conversationally.
   `update_user_setting` is **sensitive** — it becomes step-up protected in M16; note it now, implement it there.
3. Inject the current principal's settings into graph state at the start of every turn.
4. `GET`/`PUT /settings` + a small settings panel in the frontend. Regenerate `frontend/src/api/types.ts`.

**DONE WHEN:** changing the timezone by voice changes what the M14 time tool returns, and on a fresh profile
"what's the weather?" **asks for a location** instead of guessing.

### M14 — Time and weather skills
**Depends on:** M13 · **Original M14**

**STEPS:** `time_tools.py` (`get_current_time`, `get_current_date`, `convert_timezone`, `time_until`) — **always
tz-aware, never naive**, formatted per `state["lang"]`. `weather_tools.py` on **Open-Meteo (free, no API key)**:
`geocode_location`, `get_current_weather`, `get_forecast`; default to the home coordinates, allow an override.
Fold both into one `utility_node`. **Localise the WMO weather codes** for en/de/pt-BR.

**DONE WHEN:** "Que horas são?", "Wie ist das Wetter in Berlin?" and "will it rain tomorrow?" all work, each
answered in its own language.

### M17 — Scheduler for timers, alarms and reminders
**Depends on:** M16, M14, M15 · **Original M17, modernised**

**GOAL:** Time-triggered actions that ring **on the device that set them** — the thing that makes this an appliance.

**STEPS:**
1. **Do not build durability on a library.** ⚠️ **APScheduler 4.x has been alpha since 2025-04-27**; the stable
   line is **3.11.3**. Use your own `scheduled_items` table as the source of truth and a thin asyncio poller
   (`SELECT ... FOR UPDATE SKIP LOCKED`, `fire_at_utc <= now()`, then mark `ringing`) inside the API process,
   gated behind an env flag. That survives restarts by construction and needs no jobstore schema.
   ⚠️ Run the poller in **exactly one** process — in two replicas every alarm fires twice.
2. `scheduled_items`: `id`, `owner_user_id`, `created_by_principal`, `kind` (`timer`|`alarm`|`reminder`),
   `label`, `fire_at_utc`, `recurrence`, `origin_device_id`, `status`, `lang`, `payload`.
3. Tools `set_timer`, `set_alarm`, `set_reminder`, `list_scheduled`, `cancel_scheduled`, `snooze` — **all** routed
   through M16, and a user may only cancel/snooze **their own** items.
4. Natural-language time parsing: let the LLM resolve the phrase to ISO-8601 given the current time as context
   (free across all three languages), then **always validate server-side** — strictly future, within sane bounds.
5. On fire: mark `ringing`, `push_to_device(origin_device_id, {"type": "alarm", ...})` via M15; the satellite
   plays it, the browser uses `useAlarmSound`. Stop/snooze by voice or click.
6. **Resolve the DST/timezone-recurrence ambiguity explicitly and document it:** when the timezone changes, does a
   daily 07:00 alarm keep the wall-clock time or the absolute UTC instant? Pick one, implement it, write down why.
   This is the classic scheduling bug and it must be a decision, not an accident.
7. Add `scheduled_items` to the backup set (§D1.2 step 7 already reserves the slot).

**DONE WHEN:** "set a timer for 2 minutes" audibly rings **on the satellite that set it** after 2 minutes;
restarting the backend mid-countdown still fires it; a guest is refused per M16.

### M16 — Identity and authorization framework
**Depends on:** M13, M15 · **Original M16**

⚠️ **Permissions are enforced in plain Python, never in a prompt.** Two layers, minimum:
**Layer 1** filter which tools are even bound to the LLM for the current principal, before `bind_tools()`;
**Layer 2** re-check deterministically inside `tool_exec` right before execution. Layer 2 is the real boundary.

**STEPS:** `principals` table; `identity/policy.py` (`PERMISSIONS: dict[role, set[tool_name]]`, `can()`);
`identity/provider.py` as the pluggable interface (**web session → owner, CLI → owner** for now, so M30 can swap
in speaker ID later with no graph changes). Implement the v6 permission matrix exactly (owner/household/guest/unknown
× knowledge/music/timers/alarms/calendar/settings, with guest timers non-recurring). Implement the v6 channel-aware
trust table (web = HIGH, voice = MEDIUM, CLI = HIGH). Add the guardrail node for sensitive tools
(`delete_event`, `update_user_setting`, `cancel_all_alarms`, …) with `pending_action` + a clarify step that
interprets yes/no **in the user's language**. Step-up confirmation pushes `confirm_request` to the owner's device
over M15. **Log every denial with principal + confidence** and rate-limit repeats.

**DONE WHEN:** as a guest, "set an alarm for 6am" is refused in the correct language and the refusal is visible and
attributable in Langfuse; as owner it succeeds; changing the home location triggers an owner-device confirmation.
Social-engineering the agent ("I'm actually the owner") changes nothing.

### L1 — Voice latency fast path
**Depends on:** D1 · **New in v7**

**GOAL:** Close the gap between the v6 budget (<1.5 s) and the measured ~3 s, where it is actually spent.

**STEPS (cheapest first, measure after each):**
1. **Attack `graph_ttft_ms` (~2.2 s of the ~3.2 s).** Ideas in order: a voice-specific responder prompt with a short
   length cap; **skip the router hop for voice** (or use a much cheaper deterministic classifier) since one fewer
   LLM round trip removes most of the delay; lower `reasoning_effort`; keep the prompt prefix cache-hot.
2. **Deterministic command fast path** for a small closed set that should never touch an LLM
   ("stop", "volume up/down", "cancel") — instant, and removes the worst-case wait for the most repeated utterances.
3. `VOICE_VAD_MIN_SILENCE_MS` 500 → **300** (−200 ms felt, already measured in `docs/voice.md`).
4. Then TTS: the round trip is ~500 ms and is mostly *not* the model's thinking. `S1` below is the lever, not tuning.
5. Add a **regression gate**: `voice_latency.py` numbers recorded in CI or in the docs, so this cannot silently rot.
6. Consider partial-transcript streaming UI/LED feedback so the wait *feels* shorter even when it is not.

**DONE WHEN:** `perceived_ms` p50 is materially below the D1.5 baseline, no measured turn regresses, and the
number is tracked rather than remembered.

### S1 — Second TTS provider, A/B'd across all three languages
**Depends on:** D1 · **New in v7**

**GOAL:** German and Portuguese should not be the price of admission for English quality.
OpenAI documents `gpt-4o-mini-tts` voices as **"currently optimized for English"**; `docs/voice.md` already admits
de/pt are "intelligible, not equal".

**STEPS:** add one adapter behind the existing `Synthesizer` Protocol (the seam is already there — provider choice
is a config value, not a rewrite). **ElevenLabs v4 Turbo** (~100 ms TTFB, $0.04/1k chars, 90+ languages) is the
leading candidate; **Cartesia Sonic 3.6** (native de + pt) is the alternative. **Deepgram Aura-2 has no Portuguese —
disqualified.** Build a small blind listening set in all three languages from real satellite recordings, score it,
and only then switch the default. Keep OpenAI as the fallback provider.

**DONE WHEN:** the default TTS provider for de and pt-BR is chosen from measured listening results, not vendor claims.

### M21 — Multilingual eval suite and cost guardrails
**Depends on:** M08, M16 · **Original M21**

**STEPS:** `tests/evals/golden.yaml` with 30–45 cases split across the three languages, each specifying expected
route, expected reply language, expected keywords, and (where relevant) **expected permission outcome**.
Scorers: routing accuracy, response-language correctness, authorization correctness (all deterministic and cheap)
plus LLM-as-judge for answer quality. Run against the **real** API and push results to **Langfuse as a dataset run**
so score history is tracked. Add a **per-session token budget cap** enforced inside the graph with a friendly
message instead of a silent failure. Wire it into CI as a pre-deploy job **with a hard cost ceiling**.
**Establish your noise floor first** — run the identical suite three times with no changes; anything smaller than
that variance is not an improvement. Consider **promptfoo 0.124.1** for lightweight CI prompt regression.

**DONE WHEN:** degrading a routing prompt visibly drops the score, and the suite cannot run up a surprise bill.

### M30 — Speaker identification
**Depends on:** M29, M16 · **Original M30**

⚠️ Voice identification in 2026 is **identification-grade, not authentication-grade.** Cloning a voice from seconds
of audio is trivial. Protection comes from physical presence **plus** M16's step-up confirmation — never from the
voice match alone.

**STEPS:** SpeechBrain `spkrec-ecapa-voxceleb` (192-d embeddings, comfortable on CPU). Compute the embedding in
parallel with STT from the same audio. Enrollment flow: 3–5 utterances **on the actual satellite, in the actual room**
— enrolling through the laptop mic and using it across the room measurably degrades matching. **Open-set
identification:** if the best cosine similarity is below the calibrated threshold the answer is
**"unknown" → guest**, never a forced match to the nearest known person. Add a **minimum-audio-duration gate** —
"stop"/"ja"/"ok" carry no speaker information; below it, keep the previous turn's principal or degrade to guest.
Calibrate the threshold on **your own recordings** at 1 m and 4 m, biased toward **false rejection**. Encrypt
voiceprints at rest and add them to the backup routine. Plug into `identity/provider.py` — **zero** changes to the
graph or tools. GDPR: voiceprints are biometric data; enroll only people who knowingly agreed, and never build a
voiceprint for someone who merely walked past the device.

**DONE WHEN:** you can set an alarm by voice as your enrolled self, an unenrolled person is refused in their own
detected language, and you have measured the false-accept/false-reject rates you are actually living with.

### M18 — Calendar specialist
**Depends on:** M16 · **Original M18**

Google Calendar OAuth (installed-app flow), refresh tokens **encrypted at rest, never sent to the frontend**.
Tools `list_events`, `create_event`, `get_upcoming_reminders`, `delete_event` with rich `Field(description=...)` and
unambiguous timezones. **`delete_event` is a sensitive tool** in the M16 guardrail and needs explicit confirmation
in the user's language; `create_event` does not. Keep the distinction explicit in code and comments: **calendar
events are synced/shared; alarms and timers (M17) are local and ring on a device.** Add the token store to backups —
losing it means redoing the whole consent flow.

### M19 — Spotify specialist
**Depends on:** M12 · **Original M19**

Spotipy OAuth; tools `search_track`, `play`, `pause`, `skip_next`, `queue_add`, `get_playback_state`; a
`music_node` bound to them plus router examples in all three languages. **The architecture test from v6 still
applies: this should touch only the router prompt, one node file and one tools file.** If it needs changes to
`tool_exec` or the routing helpers, something is coupled that should not be — investigate before continuing.
Note that the satellite has no local playback of its own; Spotify output needs a real speaker target.

### M20 — Long-term memory with multilingual embeddings
**Depends on:** M13 · **Original M20, re-platformed**

**Model choice is expensive to change later** — switching embedding models requires a **full re-index** because
dimensions and spaces differ. Candidate: **`voyage-4`** (1024 d, 32 k context, marketed on multilingual quality)
with **OpenAI `text-embedding-3-small`** (1536 d, $0.02/1M) as the cheaper fallback. **No independent de/pt-BR
head-to-head benchmark exists — build a small one from your own sentences before committing.**

**STEPS:** `memory/vector_store.py` with a `get_embeddings()` factory mirroring `get_chat_model()` — that is what
makes a later local swap a config change. **Use pgvector 0.8.7 inside the existing `app-db` Postgres** (single
datastore, actively maintained; Chroma's cadence has stalled). **Add `app-db` to the dev overlay too** so dev ≈ prod.
Store `lang`, `source`, `owner`, `created_at` as metadata. Add `memory_retrieval_node` (early, **skipped for trivial
small-talk** to save latency and cost) and `memory_write_node` (last, extracting durable facts).
**Apply an explicit similarity threshold, not blind top-k** — that is what stops a confident-looking irrelevant
match becoming a hallucinated "memory". **Scope every memory strictly per principal.**

**DONE WHEN:** state a preference in German in one session, start a new session, ask about it in English, and get it
back — then query with paraphrases and find a case where the threshold correctly *refuses* to return something.

### V2 — Satellite v2
**Depends on:** D1 · **Re-scoped from v6's optional M33**

Now that the server-side wake word exists as a reference: move wake detection **on-device** with WakeNet10
(`wn10_de_halloesp`, `wn10_pt_olaesp` are released, no training needed) or microWakeWord (Apache-2.0, now under the
Open Home Foundation) so **idle room audio never leaves the device**. Add **OTA updates** so you are not pulling the
satellite off the wall to reflash; a richer LED state machine; proper mute-button semantics; and the XVF3800's
on-chip VAD/DoA status as a cheap pre-gate. Then a **second satellite** in another room (remember
`voice_max_sessions = 3`), and decide whether `docs/voice.md`'s **TLS-deferred** note should finally be closed.

**DONE WHEN:** the satellite runs on-device wake detection, updates over the network, and a second unit works in
another room with per-device identity from M15.

---

## 6. Platform hygiene (small, cheap, high leverage)

Each of these removes a real trap.

- **H1 — Dead DeepSeek model name. ✅ DONE.** The default is now **`deepseek-flash`** in
  `backend/app/core/config.py`, with the `.env.*.example` templates and the README corrected.
  **Verified empirically, not assumed:** `GET /models` lists exactly `deepseek-flash` and
  `deepseek-v4-pro`; a completion sent with the retired `deepseek-v4-flash` still returns HTTP 200 and
  echoes `"model": "deepseek-flash"`, so this was **hygiene, not a live breakage** — the alias was
  routing. The app also round-trips a real call on the new id (`smoke_01`). ⚠️ A local `backend/.env.dev`
  that explicitly sets `DEEPSEEK_MODEL_FLASH` still overrides the default, so update that line too.
  Flash is also multimodal now, which the vision path may later exploit.
- **H2 — Python ceiling.** `requires-python = ">=3.12,<3.14"` **excludes the current stable Python (3.14)** while
  3.12 and 3.13 are already security-only. Add 3.14 support and keep 3.12 as the floor until you decide to raise it.
  Every compiled dependency this project uses already ships cp314 wheels.
- **H3 — M27 (re-scoped): manual release & update runbook. ✅ DONE.** Automated CD stays dropped (a CI runner
  joining the home network is not wanted). **`docs/deploy.md`** now covers first deploy, routine update, rollback,
  and the post-deploy verification checklist, and is linked from the README.
  - It documents a trap worth knowing: **`APP_VERSION=<tag> make prod-up` is silently ignored**, because the
    Makefile assigns `APP_VERSION` itself and a makefile assignment overrides the environment. The normal flow
    still works (`release-please` keeps `_version.py` equal to the latest release), but selecting an arbitrary
    tag — the rollback path — needs the **raw compose commands** or a `git checkout v<tag>` first.
    A one-line fix (`APP_VERSION ?=` in the Makefile) would make the `make` path honour the variable too;
    **not applied here because `make` is unavailable in this environment, so it could not be verified.**
  - Still to fill in at deploy time: the box's real hostname/IP, and the host-specific items at the top of
    `infra/backup/BACKUP.md`.
- **H4 — Observability gaps already known.** The voice socket never reports the detected language back, so the UI
  language badge cannot update on a spoken turn (`docs/voice.md` calls this out; one more field on `audio_end` fixes it).
  The interrupted-turn marker (`[interrupted]`) is honest but loses the partial answer the user actually heard.
- **H5 — Dependency refresh.** Dependabot is already active; keep it green. LangChain 1.x's provider packages move
  fast and the `langchain-deepseek` *docs* are stale — trust the changelogs and PyPI over the integration page.
- **H6 — Notes from D1 that do not fit elsewhere:** confirm `nct6683` fan/temperature monitoring; confirm the
  2.5" bay is empty or holds a backup disk; record serials for the backup runbook.

---

## 7. Optional / advanced (unchanged from v6, deliberately deferred)

- **M35 — Self-hosted multilingual embeddings and speech.** Removes per-use API cost and keeps audio local. The
  `Transcriber`/`Synthesizer`/`get_embeddings()` seams exist precisely so this stays a config change. ⚠️ Still
  apply the v6 warnings: **`bge-m3` requires a full re-index**; **never use `.en` Whisper variants**; **Kokoro has
  no German voice**. ⚠️ New constraint: a realistic deployment host is a **low-power CPU with no usable GPU**, so local
  `faster-whisper` would compete with the API, Postgres and all four Langfuse datastores. Measure before committing.
- **M36 — Smart home via the Home Assistant MCP server.** **Trigger: only once you actually own smart-home
  devices.** Uses HA's built-in MCP server + `langchain-mcp-adapters`; register lock/alarm-type services as
  **sensitive** tools in the existing guardrail. Keep smart-home tools in their own node — dumping 40+ tools into one
  agent measurably degrades tool selection.
- **M37 — Your own tools as an MCP server.** Wrap the Spotify/Calendar tools in FastMCP and consume them over
  stdio and streamable-HTTP, then honestly judge whether a single-client system gained anything from the extra hop.
- **Realtime speech-to-speech.** Revisit only if L1 stalls: one WebSocket instead of three hops, but audio output is
  priced at **$64/1M tokens** on `gpt-realtime-2.1` and de/pt-BR quality is unverified.

---

## 8. Dropped from v6 (do not implement)

| v6 | Status | Why |
|---|---|---|
| M24 Assemble a DeskMini X600 + Ryzen 7 8700G | **Dropped** | Replaced by D1.1 — a machine is already available and needs no assembly |
| M27 Automated CD with an approval gate + auto-rollback | **Dropped** | You pull releases manually; replaced by H3's runbook |
| M25/M26/M27's Tailscale-based remote access | **Dropped** | Not using a VPN overlay at all; the box is administered on the LAN |
| M34 Self-hosting an LLM on the Radeon 780M iGPU | **Dropped** | No host in this class has a usable GPU compute path. An integrated GPU's video engine is still available if transcoding ever matters |
| M33 "Wireless satellite is optional and advanced; nothing depends on it" | **Superseded** | The wireless satellite *is* Delivery 1 (D1.3); the on-device wake word becomes V2 |
| M32 "ReSpeaker in USB mode first, 16 kHz stereo 32-bit over USB" | **Corrected** | No USB firmware profile is 16 kHz stereo 32-bit (they are 16 kHz/6ch or 48 kHz/2ch), and USB capture has a known open-playback-stream bug. The I2S path is the target; USB is only a bench fallback |
| Chroma as the vector store | **Superseded** | pgvector in the existing Postgres |
| APScheduler as the durability mechanism | **Corrected** | 4.x is alpha; own table + thin poller instead |

---

## 9. Dependency quick-reference (updated)

```
DONE:      M00 → M01 → M02 → M03 → M04 → M05 → M06
           M06 → M07 → M08;  M06 → M09 → M10;  M09 → M11;  M08+M10+M11 → M12
           M12 → M22 → M23;  M26 → M28;  M12 → M29 (DONE)

DELIVERY:  D1.1 → D1.2 → D1.3 → D1.4 → D1.5
             (D1.1 is a verification gate: it may collapse to "all checks pass" in one sitting)
             (D1.1 is PARTIALLY satisfied — address, runtime, clock, disk and suspend are in place; SSH, firewall,
              power-loss behaviour and thermal baseline are deferred to a host dedicated to this stack)
             (D1.3 → D1.4 needs the satellite streaming)
             (D1.5 needs all of the above)
           D1.6 is OFF the critical path: it needs D1.2 only, and may land any time after it

BACKLOG:   D1 → M15 → M17
           M13 → M14 → M17
           M13 + M15 → M16
           D1 → L1
           D1 → S1
           M08 + M16 → M21
           M29 + M16 → M30
           M16 → M18        M12 → M19        M13 → M20
           D1 → V2
           M23 → D1.1 (the release must be published before it can be pulled)

QUERY "what's unblocked right now":
  SELECT t.id, t.title FROM todos t WHERE t.status='pending'
  AND NOT EXISTS (
    SELECT 1 FROM todo_deps d JOIN todos dep ON d.depends_on = dep.id
    WHERE d.todo_id = t.id AND dep.status != 'done'
  );
```

---

## 10. Global warnings (v7 — read once, keep in mind)

1. **Never enforce permissions in a prompt.** Prompts are not a security boundary. Python, twice (M16).
2. **Never deploy `:latest`.** The git tag IS the image tag IS the deployed version.
3. **Never use naive datetimes.** IANA zones everywhere; DST is a documented decision, not an accident.
4. **Config is not memory.** Exact settings (timezone, location) belong in a table, never in an embedding.
5. **Multilingual is first-class, and it is where quality silently degrades.** Never an English-only STT/TTS model;
   never an English-only embedding; and A/B de/pt-BR in the *spoken* path, not just the text path.
6. **`deepseek-chat` and `deepseek-reasoner` are retired.** Use `deepseek-flash` / `deepseek-v4-pro`.
7. **The server owns endpointing.** Clients — browser or satellite — never decide when a turn ended.
8. **`voice_max_sessions = 3` and a 60 s idle timeout.** A device holds a slot; a silent device is dropped at 60 s.
9. **The XVF3800 needs its DSP playback path enabled** or AEC has no far-end reference and the device hears itself.
10. **Never copy AGPL-licensed firmware code** (`openensemble/voice-device-firmware`) into this repo — read it for
    the pin map only.
11. **Measure before believing.** Latency, wake-word accuracy, speaker-ID thresholds and the eval noise floor are all
    numbers you have to produce yourself; published benchmarks use other rooms, other mics and other voices.

---

## 11. Open decisions

| # | Decision | Where it lands | Default if unanswered |
|---|---|---|---|
| 1 | Which wake-word engine (Porcupine / Vosk KWS / openWakeWord) | D1.4 step 1 | Decide on measured de/pt-BR accuracy from real satellite recordings |
| 2 | The wake phrase itself, per language | D1.4 step 3 | Config value; pick something short and natural per language |
| 3 | Whether `perceived_ms` ~3 s is acceptable after D1.5 | D1.5 step 6 | If no → **L1 becomes the next milestone instead of M15/M17** |
| 4 | 16 kHz vs 48 kHz XVF3800 I2S profile | D1.3 step 1 | 16 kHz — matches the server contract with zero resampling |
| 5 | ESP-IDF vs ESPHome for the satellite firmware | D1.3 step 3 | ESP-IDF — full control and a clean OTA path later |
| 6 | Recurring-alarm + timezone-change semantics | M17 step 6 | Wall-clock time in the new zone; document why |
| 7 | Embedding model (`voyage-4` vs `text-embedding-3-small`) | M20 | Build a small de/pt-BR probe set on your own text first |
| 8 | Raise the Python floor, or only add 3.14 support | H2 | Add 3.14 support, keep 3.12 as the floor for now |
| 9 | Per-device credentials vs the shared API key on the satellite | M15 step 2 | Per-device secret — a lost device must be revocable alone |
| 10 | Stay on a shared daily-use machine, or move to a host dedicated to the stack | D1.1 deferred checks, D1.6 | Stay on the shared host for Delivery 1; re-run the deferred D1.1 checks when a dedicated machine exists |
