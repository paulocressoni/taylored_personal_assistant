# Soak analysis

A soak is the only test that sees a real room: a satellite streaming for days, a television talking
over it, a Wi-Fi link that drops and a user who does not know they are being measured. It is also the
easiest test to misread, because the raw material is three logs with three clocks, three formats and
one housekeeping line per second.

This page is the procedure and the dictionary. The tool is
[`backend/scripts/soak_report.py`](../backend/scripts/soak_report.py); the numbers it produces for the
wake-word milestone are in [`voice.md`](voice.md#what-was-measured-and-what-was-not).

## 1. Capture

Every soak gets its **own directory**, named after the moment it started, and all three streams are
written into it by `make soak-up`:

```
~/wake-soak/
  20261112T170218Z/          <- one soak, named for its UTC start
    satellite-soak.log       the serial console: idf.py monitor in a pty
    backend.log              docker logs -t -f, with api_key= redacted
    frontend.log             the same for nginx
    META                     what the run actually was
    report.json              written by soak_report.py
  20261109T185944Z/          <- the 47-hour soak, named for its clock anchor
  current                    a one-line pointer at the newest run
```

The directory name is the label: a log path answers "which soak is this from" without opening
anything, two captures can never overwrite, interleave with or append to one another, and
`--compare` becomes a plain file reference between two run directories.

```bash
make soak-runs                            # every capture, newest first, with its firmware
make soak-report UNTIL=… RUN=20261109T185944Z   # report an older one
make soak-report UNTIL=… COMPARE=~/wake-soak/20261109T185944Z/report.json
```

`RUN` defaults to `$(cat ~/wake-soak/current)`, so the ordinary case needs no flag. Two names to know:

- the **directory** is `<UTC start>`, `date -u +%Y%m%dT%H%M%SZ`;
- **`capture_started_utc`** in `META` is the exact moment the first byte arrived, which is what a
  server-only report uses as its window start.

| File | Produced by |
|---|---|
| `satellite-soak.log` | the serial console: `idf.py monitor` inside a pty, attached to the board on this machine |
| `backend.log` | `docker logs -t -f <backend container>`, with `api_key=` redacted |
| `frontend.log` | the same for the nginx container |

The satellite is the one stream that can be left out: `make soak-up SOAK_SATELLITE=0` starts only the
two container collectors. The report then needs a `--since` — or a `META` carrying
`capture_started_utc`, which `make soak-up` writes — and prints `n/a (no satellite log)` where the
device numbers would be. Leaving it out costs every device metric and the clock anchor, so the
default is to include it.

### The satellite stream

`backend/scripts/soak_collectors.sh` starts all three and records a process group for each, so
`make soak-stop` can end them. The satellite cannot use the container recipe, and the reasons were
measured rather than assumed:

- **`screen` cannot start this.** The recipe in
  [`satellite.md`](satellite.md#capturing-a-log-you-can-keep) is the right way for a human to *watch*
  the device, but `screen` refuses to run without a TTY (`Must be connected to a terminal.`). Behind
  `>/dev/null 2>&1` that failure is invisible and leaves a zero-byte capture.
- **`script` provides the pty** the monitor needs, from any context. Three of its defaults then
  matter: without `-f` it writes the typescript only at exit; with stdin at `/dev/null` the child sees
  EOF and exits at once (so a FIFO feeder holds stdin open); without `-a` it truncates an existing
  file.
- **The capture opens with `Script started on …` and closes with `Script done on …`** — `script`'s own
  bookkeeping, in English because the collector exports `LC_ALL=C`. The report ignores both lines.
- **Attaching resets the board.** `idf.py monitor` pulls DTR/RTS, so the satellite reboots when the
  collector starts. That is desirable at the beginning of a soak: the boot banner it prints is where
  the firmware version comes from.
- **The port is discovered, not assumed.** The collector uses `${SOAK_SATELLITE_PORT}` if set,
  otherwise the single `/dev/serial/by-id/*` device (stable across renumbering, and it carries the
  MAC). Two devices are refused rather than guessed, because a soak attributed to the wrong board is
  worse than no soak.
- **A capture is never appended by accident.** Each run has its own directory, so a new soak cannot
  touch an old one — and if you pin `RUN=<name>` to re-enter an existing one, `soak-up` refuses when
  any log already has bytes. `SOAK_APPEND=1` is the deliberate override, and it passes `-a` to
  `script` so the satellite log appends too.
- **`soak-up` waits for proof**: a boot banner and a `ws: connected` within `SOAK_SATELLITE_S`
  (default 120 s — attach runs cmake, then the chip boots, then Wi-Fi associates). If either is
  missing it prints the tail of the log and `.err`, and fails, instead of leaving a capture that looks
  healthy and is empty.

The backend and frontend collectors need two properties that a naive `nohup … &` does not give them.
To do those two by hand, the shape is:

```bash
R='s/\(api_key=\)[^ &"]*/\1REDACTED/g'
setsid nohup bash -c "docker logs -t -f taylored-assistant-prod-backend-1 2>&1 \
  | sed -u '$R' >> ~/wake-soak/backend.log" >/dev/null 2>&1 &
```

- **`-t`** puts a UTC timestamp on every line. Without it the backend log cannot be placed on a
  timeline at all.
- **`setsid`** gives the collector its own process group, which is what makes `make soak-stop` able
  to kill it later. Record the **group** id, not `$!`: with `setsid` the recorded pid is the wrapper
  and the writer is its child, which is how the first soak ended up with stale `.pid` files that
  killed nothing.
- **`sed -u`** keeps the redaction unbuffered, so a crash still leaves a readable tail.

Two rules while a soak runs:

- **Do not recreate the containers.** `docker logs -f` follows a *container*: a `up -d` that replaces
  it silently ends the capture, and you find out days later. Restarting in place is fine.
- **Do not run dev and prod together.** Both publish `langfuse-web` on `:3000`
  (`docker-compose.dev.yml`, `docker-compose.prod.yml`); the frontends use `5173` and `8080` and do
  not collide.

## 2. Serving a pre-release backend on the prod URL

The flashed satellite's server URI is a **compile-time constant**
(`SATELLITE_SERVER_URI` in its Kconfig; on this deployment
`ws://192.168.2.132:8080/ws/voice`). It cannot be pointed at a dev stack, so a soak of a branch has
exactly two options:

| Option | Cost |
|---|---|
| reflash the device with a new URI | ends the baseline the soak is comparing against, and adds a variable |
| bring the branch's backend to the prod URL | one container is rebuilt; the device never notices |

The second is what `docker-compose.soak.yml` does, and it is what the first 47-hour soak did by hand.
From the repo root:

```bash
APP_VERSION=0.4.0 make soak-up     # build this tree's backend, start on :8080, begin capture
make soak-status                   # git sha, image, container, META, collector state
make soak-stop                     # stop the capture (leaves the stack running)

make soak-up SOAK_SATELLITE=0      # the same, without the device
```

`make soak-up` **recreates the backend container**, so it also ends any capture already following
it. That is the right first step and the wrong mid-soak step — check `make soak-status` first.

What `make soak-up` does, in order:

1. **Preflights before building anything** (`make soak-preflight`). It fails in about a second,
   instead of after a three-minute image build, when the dev stack is up, `.env.prod` is missing, a
   log already has bytes, a collector is still running, or the board is absent, unreadable or held by
   a leftover monitor. A `.env.prod`-shaped remedy is printed for each. The resolved device facts go
   to `~/wake-soak/plan`, which `META` then folds in.
2. **Builds only the backend** from the working tree, tagged `taylored-assistant-backend:soak-local`.
   The frontend stays the released GHCR image — it is nginx, it owns `:8080`, and `/ws` is already
   proxied.
3. **Starts the prod stack** with the soak overlay (`--no-build`, so nothing else is compiled).
4. **Writes `META`**: git sha and dirty flag, app version, image and container ids, and the
   **effective** `VOICE_*` settings read out of the running container rather than from a file. It has
   to run *after* the stack is up, or it would describe the container it is about to replace.
5. **Starts the collectors** through `backend/scripts/soak_collectors.sh`, which records a process
   *group* — not `$!` — so `make soak-stop` can actually end the `docker logs -f | sed` pipeline, and
   which refuses to lose a capture silently when a container is recreated. It then waits for the
   satellite's boot banner and first `ws: connected`, and appends `capture_started_utc`,
   `satellite_firmware` and `satellite_idf` to `META`.

`make soak-status` reports `collecting UNMANAGED` when the capture was started by hand; that is the
honest state, because `soak-stop` cannot kill a group it did not create. For the satellite it also
distinguishes `collecting (log 3 s ago)`, `SILENT (log idle 412 s)`, `monitor exited (rc=…)`,
`capture process gone` and `DEVICE GONE` — a group that survives its writer is exactly how a dead
capture comes to look alive, so liveness is checked against the `script` process and the log's age,
never against the group alone.

**What this is not.** The image is built from the working tree: not CI-tested, not Trivy-scanned, and
it writes to the **prod volumes and the prod Postgres**. That is acceptable for a soak — the data is
what the release would write anyway — but it must not be left running as *the* deployment. Finish with
`APP_VERSION=<release> make prod-up` to return to a published image.

## 3. Freeze the window

`soak_report.py` requires `--until`, an **exclusive** UTC bound, and applies it to every section.
That is not pedantry: a running soak is a moving target, and a report whose sections were measured at
different moments cannot be compared with anything.

Recording the end of the window is therefore part of the procedure:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ | tee -a ~/wake-soak/META
```

## 4. The device clock

The serial log has no wall clock, only `I (uptime_ms)`, so every line is placed with
`boot_epoch + uptime`. The tool derives `boot_epoch` from **matched events**: a satellite
`ws: connected` and a backend `WebSocket /ws/voice … [accepted]` are the same moment seen from both
ends, so `accept − uptime` is the boot time. It takes the median of the newest pairs.

Do **not** derive it from the `START` file. On the first soak `START` was written 35 minutes before
`idf.py` ran, and the capture opened with cmake output — the naive anchor put every device line 35
minutes early, and every satellite metric with it. The tool refuses an anchor whose pairs disagree by
more than **2 s**, because placing hours of events on a guess is worse than refusing to report.

## 5. Produce the report

```bash
cd backend
uv run python scripts/soak_report.py --until 2026-11-11T17:45:00Z
# -> prints the sections below, writes ~/wake-soak/report.json

uv run python scripts/soak_report.py --dir ~/wake-soak/20261112T170218Z --until … \
  --compare ~/wake-soak/20261109T185944Z/report.json
# -> refuses if the two captures are not comparable (schema, window, threshold, firmware)

uv run python scripts/soak_report.py --until … --since 2026-11-11T09:00:00Z
# -> a server-only capture: no satellite-soak.log to anchor to
```

Exit code is **1** when a section found no source lines or the anchor is unusable, so a silent zero
cannot pass a check. `report.json` carries its own `meta` — schema version, window, anchor source,
anchor spread, git sha, app version, firmware and boot count, and every non-secret `voice_*` setting —
which is what makes `--compare` honest.

Two rows read `n/a`, and they mean different things:

| Text | Meaning |
|---|---|
| `n/a (0 matching lines)` | the log was there and nothing matched. Counts as a failure: the exit code becomes 1 |
| `n/a (no satellite log)` | this capture had no device by design. Does **not** fail the run |

`--since` defaults to `capture_started_utc` in `META`, so a server-only capture does not need a
hand-typed timestamp — retyping it is the `START` mistake in a new hat. It is required only when
neither a serial log nor a `META` exists.

**META is written once per run.** `make soak-meta` regenerates it only for a run that has none
(`SOAK_META_FORCE=1` overrides), because rewriting it after the capture would drop the facts `start`
appended and describe a container that has since been replaced.

**A rebooted device is refused.** `derive_anchor` pairs the newest connect/accept events, and a boot
resets the uptime clock, so a second boot can silently move the window start to the reboot and report
nothing about the hours before it. The tool therefore refuses a capture with more than one
`app_init: Project name:` line; `--allow-reboot` reports the segment after the last boot and says how
much it ignored.

## 6. Metric dictionary

Units, the line each metric comes from, its denominator, and the mistake it exists to catch.
Percentiles are **linear interpolation on the (n−1) basis** everywhere; nearest-rank would report a
different p50 on the same data.

| Section | Metric | Source line | Denominator | Why it matters |
|---|---|---|---|---|
| Sessions | `device sessions ended` | `session ended after N s` | — | the device's own count of socket lifetimes |
| Sessions | `loss signature` | `transport_poll_write` | losses | one per loss means a single signature: a stalled write, not a protocol error |
| Sessions | `no close frame received` | `close frame` | losses | zero proves the peer never closed cleanly, so the teardown is local or on the path |
| Sessions | `uplink frames failed` | `failed=1` | losses | the counter that increments while the transport is down |
| Sessions | `p50` / `p90` / `max` | duration list | `n=` | p50 near 60 s means idle timeouts, not user behaviour |
| Sessions | `>=1790 s sessions` | duration list | `n=` | sessions clustering on a round number name a timer, not jitter |
| Sessions | `busiest hour` | loss timestamps | — | separates "flaky link" from "someone was using it" |
| Gate | `detections`, `score p50/p90` | `wake word detected (score=…)` | detections | the margin over `VOICE_WAKE_WORD_THRESHOLD` |
| Gate | `accepted turns` | `voice turn accepted (wake_lead=…)` | turns | how many detections became work |
| Gate | `one-breath turns` | negative `wake_lead` | turns | the phrase and the command in one breath — the common case |
| Gate | `rejections` + buckets | `ignored: no wake word (… since_arm=…)` | rejections | `never` = asleep; `0.5-6 s` = permit spent; `10-60 s` = you paused |
| Gate | `rejected audio` | `speech=` of rejections | turns | the saving: audio that never reached STT |
| Latency | `stt_ms`, `graph_ttft_ms`, `tts_ttfb_ms`, `first_audio_ms`, `turn_ms` | `voice turn timing` | turns with audio | `first_audio_ms` is what the user waits for |
| Playback | `size p50/p90/max` | `reply audio: N bytes` | replies | at 32 kB/s, compare against the ring capacity |
| Playback | `ring full (truncated)` | `ring full` | replies | the answer was longer than the ring: deterministic truncation |
| Playback | `underruns` | `playout ran dry` | replies | audible gaps and clicks |
| Device | `LED transitions` | `leds: state -> N` | — | 1 IDLE, 2 LISTENING, 3 THINKING, 4 SPEAKING |
| Device | `boots` | `app_init: Project name:` | — | more than one means the uptime clock restarted; the anchor refuses |
| Device | `transcript frames` | `heard:` | — | what the device actually received |
| Server errors | `ASGI exceptions` | `Exception in ASGI application` | closed sessions | must be one per vanished client at most, never more |

## 7. Comparing two soaks

1. One variable per soak. Two changes at once and the delta is uninterpretable.
2. Keep `META` with the logs, and keep `report.json` with the capture it describes.
3. `--compare` prints `*` next to every metric that moved. It refuses outright when the schema,
   the window, the anchor source, the firmware or the wake-word threshold/window differ — a "delta"
   across different settings is not a delta. A firmware bump is a bigger variable than any of them,
   so it is refused last: `--allow-firmware-change` says you meant it.

## 8. Soak #2 wants labelled utterances

Scores are not labels. A score distribution says how confident the detector was; only a recording with
a known answer says whether it was right. Before the next soak, record on the device:

- 10 real wakes ("Hey Jarvis" said normally, from the intended distance),
- 10 near misses ("hey service", "okay Travis", "hey Jervis"),

and keep them with the capture. That is what turns detections into precision and recall; without it
the honest report stays "the gate rejects the room and accepts the phrase".
