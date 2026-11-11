# Soak analysis

A soak is the only test that sees a real room: a satellite streaming for days, a television talking
over it, a Wi-Fi link that drops and a user who does not know they are being measured. It is also the
easiest test to misread, because the raw material is three logs with three clocks, three formats and
one housekeeping line per second.

This page is the procedure and the dictionary. The tool is
[`backend/scripts/soak_report.py`](../backend/scripts/soak_report.py); the numbers it produces for the
wake-word milestone are in [`voice.md`](voice.md#what-was-measured-and-what-was-not).

## 1. Capture

Three files in one directory (default `~/wake-soak/`):

| File | Produced by |
|---|---|
| `satellite-soak.log` | `screen -L -Logfile … -S soak idf.py -p /dev/ttyACM0 monitor` — see [`satellite.md`](satellite.md#capturing-a-log-you-can-keep) |
| `backend.log` | `docker logs -t -f <backend container>`, with `api_key=` redacted |
| `frontend.log` | the same for the nginx container |

The backend and frontend collectors need two properties that a naive `nohup … &` does not give them.
`make soak-up` (next section) starts them through `backend/scripts/soak_collectors.sh`; to do it by
hand, the shape is:

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
```

`make soak-up` **recreates the backend container**, so it also ends any capture already following
it. That is the right first step and the wrong mid-soak step — check `make soak-status` first.

What `make soak-up` does, in order:

1. **Refuses if the dev stack is up.** Both stacks publish `langfuse-web` on `:3000`; the frontends
   use `5173` and `8080` and do not collide.
2. **Builds only the backend** from the working tree, tagged `taylored-assistant-backend:soak-local`.
   The frontend stays the released GHCR image — it is nginx, it owns `:8080`, and `/ws` is already
   proxied.
3. **Starts the prod stack** with the soak overlay (`--no-build`, so nothing else is compiled).
4. **Writes `META`**: git sha and dirty flag, app version, image and container ids, and the
   **effective** `VOICE_*` settings read out of the running container rather than from a file.
5. **Starts the collectors** through `backend/scripts/soak_collectors.sh`, which records a process
   *group* — not `$!` — so `make soak-stop` can actually end the `docker logs -f | sed` pipeline, and
   which refuses to lose a capture silently when a container is recreated.

`make soak-status` reports `collecting UNMANAGED` when the capture was started by hand; that is the
honest state, because `soak-stop` cannot kill a group it did not create.

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

uv run python scripts/soak_report.py --until … --compare ~/wake-soak/soak-01/report.json
# -> refuses if the two captures are not comparable (schema, window, threshold)
```

Exit code is **1** when any section found no source lines or the anchor is unusable, so a silent zero
cannot pass a check. `report.json` carries its own `meta` — schema version, window, anchor spread,
git sha, app version and every non-secret `voice_*` setting — which is what makes `--compare` honest.

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
| Device | `transcript frames` | `heard:` | — | what the device actually received |
| Server errors | `ASGI exceptions` | `Exception in ASGI application` | closed sessions | must be one per vanished client at most, never more |

## 7. Comparing two soaks

1. One variable per soak. Two changes at once and the delta is uninterpretable.
2. Keep `META` with the logs, and keep `report.json` with the capture it describes.
3. `--compare` prints `*` next to every metric that moved. It refuses outright when the schema,
   the window or the wake-word threshold/window differ — a "delta" across different settings is not a
   delta.

## 8. Soak #2 wants labelled utterances

Scores are not labels. A score distribution says how confident the detector was; only a recording with
a known answer says whether it was right. Before the next soak, record on the device:

- 10 real wakes ("Hey Jarvis" said normally, from the intended distance),
- 10 near misses ("hey service", "okay Travis", "hey Jervis"),

and keep them with the capture. That is what turns detections into precision and recall; without it
the honest report stays "the gate rejects the room and accepts the phrase".
