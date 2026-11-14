# Voice pipeline (M29) — speech in, speech out

A spoken question in German, Portuguese or English gets a spoken answer in the **same**
language, over a long-lived WebSocket that serves many turns. This is the guide to the
contract, the audio decisions, the measured latency, and what the hardware will need.

Two clients speak the same contract: the **browser** (`frontend/src/hooks/useVoiceSession.ts`)
and a **Python device simulator** (`backend/scripts/smoke_09_voice_ws.py`) that stands in
for the ESP32/reSpeaker until the firmware exists.

## What M29 added

| Piece | Files |
|---|---|
| Long-lived socket | `WS /ws/voice` — `backend/app/api/routes.py`, `backend/app/voice/session.py` |
| Server-side endpointing | `backend/app/voice/vad.py` (vendored Silero VAD, ONNX) |
| Speech-to-text | `backend/app/voice/stt.py` (`Transcript`, `Transcriber` Protocol, OpenAI-compatible adapter) |
| Text-to-speech | `backend/app/voice/tts.py` (`Synthesizer` Protocol, sentence splitter) |
| Audio plumbing | `backend/app/voice/audio.py` (framing, WAV, resampling incl. a streaming resampler) |
| Providers + slots | `backend/app/voice/registry.py` |
| Per-stage timings | `backend/app/core/timing.py` + `stamp_voice_timing` in `backend/app/core/observability.py` |
| Trace waterfall | `stt` / `tts` child spans via `child_observation` — see [observability.md](observability.md) |
| Spoken-answer prompt | `VOICE_STYLE_ADDENDUM` in `backend/app/prompts/assistant.py` |
| Browser client | `useVoiceSession.ts`, `MicButton.tsx`, `lib/socket.ts`, `lib/voice.ts`, `public/worklets/pcm-capture.js` |
| Tooling | `scripts/make_voice_fixtures.py`, `smoke_08_voice_pipeline.py`, `smoke_09_voice_ws.py`, `voice_latency.py` |

Endpointing is **authoritative on the server**. A browser cannot be trusted to decide when
a sentence ended, and the browser and the device must behave identically — so the client
never runs its own VAD; it reacts to the server's `speech_start` frame.

## The shape of a turn

```
mic (16 kHz mono s16le, 20 ms frames)
  → Silero VAD + endpointer            → speech_start, then Utterance
  → STT (Groq whisper-large-v3-turbo)  → transcript frame
  → the EXISTING graph (channel="voice", same session_id/thread_id as typed turns)
  → sentence splitter → TTS (gpt-4o-mini-tts, PCM) → binary frames
  → audio_end
```

The reply is split into **sentences and each is synthesised while the graph is still
producing the next**, so the first audio byte does not wait for the whole answer. Voice
turns are persisted into the same thread as typed ones: speech and typing share one
memory, and the transcript/reply appear in the chat history.

## The frame contract

Binary frames only ever carry audio: **mono, little-endian signed 16-bit PCM**.

### Client → server

| Frame | Content | Notes |
|---|---|---|
| start | `{"session_id": "...", "device_id": "...", "output_sample_rate": 16000 \| 24000, "wake_word": "auto"}` | **Must be the first message.** Validated by `VoiceStartRequest`; a bad one is refused with `1003`. Everything but `session_id` is optional (`24000` and `"auto"` defaults) |
| audio | binary PCM at `voice_input_sample_rate` (16 kHz) | Any frame size; the endpointer re-frames into 512-sample windows |
| stop | `{"type": "stop"}` | Ends the session; any turn in flight is cancelled. Any other `type` is logged and ignored |

### Server → client

| Frame | Content | Notes |
|---|---|---|
| `ready` | `{"input_sample_rate", "output_sample_rate", "format", "wake_word"}` | Once, after the start frame. `wake_word` echoes whether **this** session is gated. **Audio sent before this is read as the start frame** and the socket is refused |
| `speech_start` | `{"type": "speech_start"}` | Every onset on an un-gated session; on a gated one, the wake detection and any onset that happens inside the permit. Doubles as **"stop playing what you have buffered"** — this is the barge-in signal |
| `transcript` | `{"type": "transcript", "text": "..."}` | After STT, with the wake phrase already stripped. Not sent for a bare wake word or a cancel phrase |
| `token` | `{"type": "token", "content": "..."}` | The assistant's reply as text, token by token (the audio is the primary output) |
| audio | binary PCM at the negotiated output rate | The spoken answer, streamed as it is synthesised |
| `audio_end` | `{"type": "audio_end"}` | The turn's audio is complete |
| `error` | `{"type": "error", "detail": "..."}` | Transcription failed, the turn failed, or the turn timed out |

### Refusals and close codes

| Code | When |
|---|---|
| `1008` | Missing/wrong API key, or `VOICE_ENABLED=false` (an `error` frame explains which) |
| `1003` | The start frame is missing, not JSON, or fails validation |
| `1013` | Rate limit exceeded, or all voice slots are busy (`VOICE_MAX_SESSIONS`) |
| `1011` | Unexpected server error (the traceback is logged) |

## Why the audio contract looks like this

- **Raw PCM, not opus/webm.** The runtime image installs **no apt packages and has no
  ffmpeg**, so the server cannot decode a container. PCM is what is left — and it is what
  the hardware produces anyway.
- **16 kHz in.** It is the rate the VAD window is defined at, the rate the reSpeaker
  XVF3800's I2S interface runs at, and a rate browsers can capture natively.
- **Two output rates.** A browser asks for **24 kHz** (the TTS provider's native rate, so
  no conversion at all); the reSpeaker asks for **16 kHz** (its I2S rate, one resampler).
  `StreamingResampler` converts per chunk with an anti-alias filter and no lookahead gap,
  and it is byte-identical to a single-shot `resample_pcm` of the whole stream.
- **`device_id`** tells the backend which device a session belongs to (the same field the
  text channel sends).

## Endpointing and barge-in

`voice_vad_min_silence_ms` (500 ms) is the main dial: it is deliberately 5× the upstream
default because waiting out a thinking pause is better than cutting the user off — and it
is real, perceived latency, paid on every turn.

The reader loop never blocks on the answer, which is what makes barge-in possible: audio
keeps arriving while TTS streams, so the endpointer can detect a new onset and cancel the
turn in flight (`cancel()` on the turn task, plus a `speech_start` frame so the client
flushes its playback queue). Cancellation is the only interruption path — `CancelledError`
derives from `BaseException`, which is what stops the session's broad handler from eating
it.

### What a barge-in leaves in the thread

A cancelled turn must not leave the conversation question-answer unbalanced. LangGraph
commits a run's **input as its first checkpoint**, so the moment a turn enters the graph its
`HumanMessage` is durable — but the assistant's `AIMessage` is only committed when that node
returns. Cancelling a turn mid-node therefore used to persist a question with no answer:
`GET /sessions/{id}/history` showed it unanswered, and the *next* voice turn loaded that
checkpoint and handed the model an unanswered user turn.

`VoiceSession._close_interrupted_turn` closes the gap when a turn's teardown runs. It asks
the thread for its snapshot and looks at `StateSnapshot.next`: an interrupted run is exactly
one that stopped before `END`, so `next` is non-empty. When it is, the turn is given a short
`AIMessage` — `"[interrupted]"` (`INTERRUPTED_MARKER`) — written through `aupdate_state`. A
turn that answered, or one cancelled before the graph ever started (nothing was persisted),
has nothing pending and is left alone.

Two consequences worth knowing:

- The marker is a real persisted assistant message, so a barge-in shows up in the chat
  history as `[interrupted]`. That is deliberate: it is honest — the user did cut the answer
  off — and it keeps the model window well-formed. It is never spoken.
- Barge-in waits for the cancelled turn to finish unwinding before the reader loop accepts
  more audio. `speech_start` still goes out first, so the client cuts playout immediately;
  the wait only serialises the teardown, which is what stops the repair writing to the
  checkpointer while the next turn is already streaming into it.

A turn that fails is reported and the socket stays open: a provider error must never end
the conversation.

## Latency: budget vs measured

The original budget, from end-of-speech to the first audio byte:

| Stage | Budget |
|---|---|
| STT round trip | 150–350 ms |
| Graph → first assistant token | 300–600 ms |
| TTS first PCM byte | 200–400 ms |
| **Total** | **0.65–1.35 s** (target < 1.5 s) |

Measured with `scripts/voice_latency.py --runs 5 --warmup 1` (fixture `voice_en.wav`,
5 measured turns, 1 discarded).

### Baseline, before the eager first chunk

Taken on the tree *before* `VOICE_TTS_FIRST_CHUNK_MIN_CHARS` / `_MAX_CHARS` existed, so it
is the "before" column of the comparison. The "after" column is the third table below.

| Stage | p50 | p90 |
|---|---|---|
| `stt_ms` | 272 ms | 278 ms |
| `graph_ttft_ms` | 2225 ms | 2683 ms |
| `tts_ttfb_ms` | 607 ms | 909 ms |
| ↳ `generation_ms` (first→last token) | 90 ms | 132 ms |
| ↳ `tts_call_ms` (last token→first audio) | 496 ms | 825 ms |
| **`first_audio_ms`** | **3161 ms** | **3867 ms** |
| `perceived_ms` (from the caller's last frame) | 3455 ms | 4153 ms |

`tts_call_ms` — last token to first audio byte — is where the eager first chunk has to show
up: cutting the opening chunk early starts the TTS request at the clause break instead of at
the final token, so `tts_call_ms` loses exactly the **generation time remaining after that
break**, and it can go *negative* only when that remainder exceeds the whole TTS round trip
(the tool's own docstring: a negative value means a sentence was already being spoken before
the last token arrived).

That makes `generation_ms` — not the 496 ms TTS round trip — the ceiling on what chunking
can reclaim. This page got that wrong at first, and it is the easiest number here to
misread: the TTS call still takes its full round trip, it merely *starts earlier*. A reply
that is one short clause-less sentence reclaims nothing at all, because there is no break
inside it to cut at.

### Config-only experiment: `VOICE_VAD_MIN_SILENCE_MS=300`

Same command, still on the pre-change tree, with `$env:VOICE_VAD_MIN_SILENCE_MS='300'`
on Windows or `VOICE_VAD_MIN_SILENCE_MS=300` prefixed to the command on Linux/macOS. No
code change.

| Stage | p50 | p90 |
|---|---|---|
| `stt_ms` | 279 ms | 287 ms |
| `graph_ttft_ms` | 2207 ms | 2992 ms |
| `tts_ttfb_ms` | 599 ms | 743 ms |
| ↳ `generation_ms` (first→last token) | 78 ms | 119 ms |
| ↳ `tts_call_ms` (last token→first audio) | 526 ms | 624 ms |
| **`first_audio_ms`** | **2970 ms** | **3876 ms** |
| `perceived_ms` (from the caller's last frame) | 3055 ms | 3961 ms |

`first_audio_ms` did not move — 2970 vs 3161 ms p50 is run-to-run noise, and its p90 got
*worse*. The gain lands entirely in `perceived_ms`, 3455 → 3055 ms p50. That is the
definitional point below: the tool stamps `first_audio_ms` after endpointing has already
finished, so only `perceived_ms` can see a shorter silence window.

### After the eager first chunk (`VOICE_VAD_MIN_SILENCE_MS=300`)

Same command, same silence window, but on the tree *with* `VOICE_TTS_FIRST_CHUNK_MIN_CHARS` /
`_MAX_CHARS` in place — the like-for-like "after" column.

| Stage | p50 | p90 |
|---|---|---|
| `stt_ms` | 282 ms | 291 ms |
| `graph_ttft_ms` | 2452 ms | 2861 ms |
| `tts_ttfb_ms` | 623 ms | 676 ms |
| ↳ `generation_ms` (first→last token) | 76 ms | 120 ms |
| ↳ `tts_call_ms` (last token→first audio) | 537 ms | 600 ms |
| **`first_audio_ms`** | **3419 ms** | **3685 ms** |
| `perceived_ms` (from the caller's last frame) | 3499 ms | 3766 ms |

**No resolvable difference — and on this fixture there cannot be one.** `tts_call_ms` came
back at 537 ms p50 against 526 ms before, and `first_audio_ms` moved 449 ms p50 the wrong way
(2970 → 3419 ms), driven by `graph_ttft_ms` (2207 → 2452 ms) — the model's own timing, not
the splitter. Two reasons:

- The fixture asks `What is the capital of France?`, and the reply this page has always used
  for it — `The capital of France is Paris.` — is one short sentence. Its entire
  `generation_ms` is 76 ms p50 / 120 ms p90, and that is the structural ceiling on any
  saving here (see the paragraph under the baseline table). 120 ms cannot be resolved by
  5 runs whose own spread is ~450 ms.
- Even a longer sentence only gains when a clause break lands *early* in the generation, and
  that reply contains no clause break at all.

So the eager first chunk is **verified by the splitter tests, not by this tool**, and the
honest number to publish for it is "below the noise floor of this fixture". Measuring it
would need a reply whose generation runs well past its first clause break — a long or
multi-clause answer — which is a different measurement to set up.

Read this honestly:

- **STT meets its budget.** The 1.2 s seen in the very first cold run was TCP/TLS
  handshake, not transcription — which is exactly why the tool discards a warm-up run.
- **The graph was ~70% of the wait.** `graph_ttft_ms` was 2225 ms p50 of a 3161 ms p50
  `first_audio_ms` in the pre-collapse measurement, when it covered **three sequential
  model round-trips** (router → specialist → responder). After the single-assistant
  collapse the same fixture measures `graph_ttft_ms` p50 **0.57–0.76 s** and
  `perceived_ms` p50 **1.91–2.04 s** across three local sets. The graph is no longer the
  dominant cost; STT, TTS TTFB and the 500 ms VAD window are. The <1.5 s target is close
  but not yet met.
- **The first chunk is eager; later chunks are not.** Only the reply's FIRST chunk may be
  cut before a sentence ends: once `VOICE_TTS_FIRST_CHUNK_MIN_CHARS` (12) characters are
  buffered it takes the first clause break (comma, semicolon, colon, em dash), and if none
  arrives it falls back to a word boundary at `VOICE_TTS_FIRST_CHUNK_MAX_CHARS` (80) so a
  long comma-less sentence cannot stall audio. Later chunks keep the sentence rule
  (`VOICE_TTS_MIN_CHUNK_CHARS`), because by then the answer is already playing and a
  mid-sentence cut would cost prosody and an extra request for no felt gain. So
  `The kitchen light is on, and the temperature is 21.5 degrees.` starts speaking its first
  24 characters while the sentence is still being written, whereas a short comma-less
  sentence such as `The capital of France is Paris.` is unchanged — there is no break inside
  it to take. Note the decomposition: `tts_ttfb_ms` is `generation_ms` + `tts_call_ms`
  (90 + 496 ≈ 607 ms), so the "waiting for the reply" part of it is only ~90 ms — the TTS
  round trip, not the reply, is what `tts_ttfb_ms` is mostly made of.
- **`first_audio_ms` hides the whole endpointing wait; `perceived_ms` hides none of it.**
  `first_audio_ms` is stamped from the server's own `speech_end`, which the endpointer only
  sets *after* the trailing silence window has elapsed, so what the caller actually waits is
  `first_audio_ms` + `VOICE_VAD_MIN_SILENCE_MS`. The 300 ms experiment above shows exactly
  that: `first_audio_ms` did not move and `perceived_ms` did. Quote `perceived_ms` when the
  question is "what did the caller feel"; quote `first_audio_ms` when comparing pipeline
  stages.
- **`1.5 s` is not reachable with a three-hop graph and a ~500 ms TTS round trip.** That
  is a decision to take, not a bug to fix: either the graph gets a voice fast path, or this
  page publishes a budget it actually meets.

### `perceived_ms` exists only in the measurement tool

Every `perceived_ms` in the tables above is a **client-side** number:
`scripts/voice_latency.py` measures it from the last frame the caller sent to the
first audio byte it received. The server cannot observe the instant the caller
stopped talking, so it cannot stamp that instant: `perceived_ms` is not trace
metadata, it never reaches Langfuse, and **no dashboard or alert will ever show
it**. Use `first_audio_ms` instead as the server-visible budget — it is one of the
six stage scalars on the trace root, described in
[observability.md](observability.md) → "The voice trace waterfall" and inventoried
in [dashboards.md](../infra/observability/dashboards.md) §6, which states the same
gap for anyone arriving from the dashboard side.

Read the pair for what they are: `first_audio_ms` when comparing pipeline stages
or building a panel, `perceived_ms` when the question is "what did the caller
feel". The known gap between them is the VAD silence window and the endpointing
wait that follows it — both invisible in traces, and both paid by the caller
before the server's clock even starts.

### What the listener heard, and why the metrics cannot show it

Reported after listening on the browser client: cutting the reply at clause and sentence
boundaries **improves the fluency** of the voice — the breaks now land at sensible speech
pauses rather than in what the listener heard as arbitrary places — while short audio breaks
remain audible, most noticeably right after the opening chunk. This is a listener's report,
not a measurement: it is the *quality* half of the change, and the tables above are the
latency half, which is why they show nothing.

The mechanism is worth knowing before polishing it, because it says where *not* to look.
Consecutive chunks are one continuous PCM stream on the wire — the per-turn
`StreamingResampler` carries its filter state across chunk boundaries, and the browser
schedules each buffer to start exactly where the previous one ended — so there is no gap in
the transport. The seam is in the synthesis: every chunk is its own TTS request, so the
engine renders `The kitchen light is on,` as a *complete* utterance with final intonation and
its own tail, and the next request starts fresh. A break at a comma is therefore heard as a
short pause in the middle of a sentence.

That also explains the shape of the report. The splitter itself can only tear a word when
`VOICE_TTS_MAX_CHUNK_CHARS` characters pass with no space in them at all — the last resort in
`_whitespace_split` — so a break heard *inside* a word is far more likely to be this audible
seam than a mid-word cut.

Note the honest tension: the seam the listener notices most is right after the first chunk,
which is the boundary this change introduces. It did not create the seams — the sentence rule
makes them too — it moved them somewhere a listener accepts, and added one more. Polishing
that means fewer, longer opening chunks, or a provider whose prosody continues across
requests.

Levers, roughly in order of value: a faster TTS provider (both vendors sit behind the
`Synthesizer` Protocol — a config switch plus one adapter), a voice fast path in the graph
(the ~70% above), then `VOICE_VAD_MIN_SILENCE_MS` 500 → 300 (a straight −200 ms of felt
latency: the two baseline runs show `perceived_ms` at 3455 vs 3055 ms p50), and last the
chunking bounds — `VOICE_TTS_FIRST_CHUNK_*` and `VOICE_TTS_MIN_CHUNK_CHARS`. The chunking
levers are the smallest of the four by construction: they can only reclaim the generation
time *remaining after the first break*, which on this fixture's one-sentence answers is under
120 ms.

### Seeing one turn, not just its totals

The tables above are per-stage AGGREGATES: they say a turn cost ~3.2 s at p50, not where that
particular turn's time went. For a single turn, open its trace in Langfuse: every spoken turn
draws an `stt` span around the transcription and one `tts` span per sentence, each carrying
its own `ttfb_ms`, `bytes` and `index`, so the overlap of sentence synthesis with graph
generation is visible instead of inferred. The six scalars in the tables stay on the trace
root — the spans are an extra dimension, not a replacement.

See [observability.md](observability.md) → "The voice trace waterfall" for the span
vocabulary and what each attribute means, and
[dashboards.md](../infra/observability/dashboards.md) §5.8/§5.9 for the panels that
read the spans and the turn root — plus the barge-in rate, which no panel can show.

## Configuration

Everything lives in `backend/app/core/config.py` and is set through the env templates
(`backend/.env.dev.example`, `backend/.env.prod.example`). Voice is **off by default**;
`VOICE_ENABLED=true` requires an STT key, a TTS key and a voice for every supported
language, or the app refuses to boot.

| Setting | Default | Why it matters |
|---|---|---|
| `VOICE_ENABLED` | `false` | Off means `/ws/voice` is refused with 1008 and no ONNX session is ever built |
| `VOICE_STT_PROVIDER` / `_MODEL` / `_BASE_URL` / `_API_KEY` | `groq`, `whisper-large-v3-turbo`, Groq's OpenAI-compatible URL | Anything speaking `/v1/audio/transcriptions` works |
| `VOICE_STT_LANGUAGE_HINT` | `true` | Biases transcription with the language the previous turn resolved to |
| `VOICE_TTS_PROVIDER` / `_MODEL` / `_API_KEY` | `openai`, `gpt-4o-mini-tts` | PCM streaming, 24 kHz |
| `VOICE_TTS_VOICES` | `{"en": "marin", "de": "cedar", "pt-BR": "coral"}` | The single source of truth; `voice_for()` falls back to `en` |
| `VOICE_TTS_INSTRUCTIONS` | short spoken-style hint | Sent with every request — keep it short |
| `VOICE_TTS_MIN_CHUNK_CHARS` / `_MAX_CHUNK_CHARS` | `40` / `240` | How the reply is cut into speakable chunks **after the first one** |
| `VOICE_TTS_FIRST_CHUNK_MIN_CHARS` / `_FIRST_CHUNK_MAX_CHARS` | `12` / `80` | The **first** chunk is cut earlier on purpose: at a clause break once 12 characters are buffered, or at a word boundary by 80 characters when no clause break arrives. This is what lets a one-sentence reply start playing before it is finished; set the min equal to `VOICE_TTS_MIN_CHUNK_CHARS` and the max equal to `VOICE_TTS_MAX_CHUNK_CHARS` to get the old behaviour back |
| `VOICE_INPUT_SAMPLE_RATE` | `16000` | Pinned to the VAD window by a config validator |
| `VOICE_OUTPUT_SAMPLE_RATE` | `24000` | Browser default; a device asks for 16000 in its start frame |
| `VOICE_VAD_*` | `0.5` / `0.35` / `150` / `30` / `500` / `30` | Threshold, negative threshold, min speech, speech pad, min silence, max utterance |
| `VOICE_IDLE_TIMEOUT_SECONDS` | `60` | A socket that sends neither audio nor a keepalive for this long is dropped, releasing its slot |
| `VOICE_MAX_SESSIONS` | `3` | Concurrent sessions; each holds a provider quota and its own ONNX session |

## Tooling

```powershell
cd backend
$env:ENV="dev"

# 1. Synthesize the spoken fixtures ONCE (spends a little OpenAI credit).
uv run python scripts/make_voice_fixtures.py      # tests/fixtures/voice_{en,de,pt}.wav

# 2. The whole pipeline with no socket involved: fixture in, reply WAV out + timings.
uv run python scripts/smoke_08_voice_pipeline.py --lang en

# 3. The device simulator, against a running server (the ESP32 contract, in Python).
uv run uvicorn app.main:app --reload               # in another terminal
uv run python scripts/smoke_09_voice_ws.py --lang de --out reply_de.wav

# 4. The measurement that decides whether the budget above is met.
uv run python scripts/voice_latency.py --runs 5 --warmup 1
```

On Linux/macOS prefix each `uv run` command with `ENV=dev` instead of the `$env:ENV="dev"`
line (e.g. `ENV=dev uv run python scripts/smoke_08_voice_pipeline.py --lang en`).

`smoke_08` drives the **real** `VoiceSession` (real VAD, real providers, real resampler)
and writes the reply as a WAV, so the only thing left to explain a slow turn is the
pipeline. `voice_latency` builds the providers and the graph **once** — that is what keeps
the TLS connections warm — and reports p50/p90 per stage, with `_TimedGraph` splitting
`graph_ttft_ms` into per-node model time and the recorder splitting `tts_ttfb_ms` into
generation and the TTS round trip. Both are integration-only: they cost money and need
network.

### Browser microphone: a secure context is mandatory

The browser only exposes `navigator.mediaDevices` in a **secure context** — `https://`,
`http://localhost` or `http://127.0.0.1`. A plain-HTTP LAN address is not one, and Firefox
then leaves `navigator.mediaDevices` **undefined**, so `getUserMedia` throws before any
permission prompt exists. The hook now name-checks the condition and reports it, instead of
blaming a refused permission.

Consequences for testing a deployment on the LAN:

- `http://localhost:8080` **on the host running the stack** works — that address is a secure
  context even though the site is served over HTTP.
- `http://<lan-ip>:8080` from another device cannot use the microphone. Use
  `backend/scripts/smoke_09_voice_ws.py` to exercise the socket from a dev machine, or put the
  UI behind HTTPS.
- HTTPS termination is deliberately not part of the image yet: it needs a per-device trust
  decision (mkcert CA installed, or one self-signed warning accepted). The satellite path is
  unaffected — firmware connects with plain `ws://` and has no secure-context rule.

## Tests

`backend/tests/` covers the pipeline with no network and no keys: `test_voice_audio.py`
(+ `test_voice_resample_stream.py`), `test_voice_vad.py`, `test_voice_stt.py`,
`test_voice_tts.py`, `test_voices.py`, `test_voice_timing.py`, `test_voice_registry.py`,
`test_voice_session.py`, `test_voice_ws.py`, `test_voice_model_assets.py`, and
`test_voice_fakes.py` (the doubles themselves). Live-provider runs are
`pytest.mark.integration` and are excluded by default.

The frontend covers only the pure logic (`src/lib/__tests__/voice.test.ts`): jsdom has no
`AudioContext`, so the playback scheduler is a pure function taking the clock as an
argument, and the Web Audio wiring itself is verified by hand.

## Hardware notes (reSpeaker XVF3800 + XIAO ESP32S3)

The firmware lives in `firmware/satellite-xvf3800-xiao/`. Two documents cover it:
[`firmware/README.md`](../firmware/README.md) is the reference — pin map, XVF3800
flashing procedure, LED table, log lines and bring-up results — and
[`docs/satellite.md`](satellite.md) is the guided tour: toolchain from zero, the firmware's
task model, the bugs behind each design choice, and a troubleshooting catalogue. The Python
simulator speaks the same contract, so the protocol can be exercised with no board attached.
What the board requires:

- **I2S firmware ≥ v1.0.9**, host at **16 kHz, stereo, 32-bit** samples; the host reads the
  left channel (both I2S output channels carry ASR beam 0) and downshifts 32 → 16 bit for
  the wire.
- **Playback must use the XVF3800 path, not the DAC bypass**, with `I2S_DAC_DSP_ENABLE=1`,
  `AUDIO_MGR_FAR_END_DSP_ENABLE=1` and a tuned `AUDIO_MGR_SYS_DELAY`. That is what gives
  the DSP a far-end reference to cancel the speaker with — without it, barge-in and
  server-side VAD both hear the assistant's own voice.
- **Keepalive** roughly every 20 s (audio or a no-op frame): the idle timeout is 60 s and
  the device streams nothing while it is quiet.
- **`MUTE_FUNCTION_ENABLE=0`** turns the mute button into a pure event source so the host
  owns mute semantics; it is not persisted to flash, so re-send it after boot.
- The device declares its own rates in the start frame (`output_sample_rate: 16000`) and
  its serial number as `device_id`.

## Wake word (server-side, openWakeWord)

The satellite streams continuously, so without a gate every sound in the room becomes a paid
transcription and a graph run. The gate lives on the **server** — one implementation for browser,
simulator and device, tunable without a reflash, and the firmware stays as dumb as the rest of
this document insists it is.

**"Hey Jarvis", English only, one model.** The three ONNX graphs are vendored under
`backend/app/voice/models/openwakeword/` (provenance, licence and caller obligations in the README
there) and driven by the `onnxruntime` this service already depends on. Measured on the reference
host: **1.9 ms per 80 ms hop, ~40× realtime, ~2.5 % of one core per session** (single-threaded).

### What a turn needs

```
audio → wake detector (every 80 ms hop) ── detection ──▶ arm a permit
      ↘ Silero VAD + endpointer ── onset ──▶ forwarded only while a permit is open
                                 └─ segment ──▶ admitted only if it STARTED inside the permit
                                                 └─▶ STT → strip the phrase → graph → TTS
```

- The permit is **one-shot**: accepting a segment closes it, a bare "Hey Jarvis" re-arms it, and a
  completed answer does not. That is what keeps a television from cutting an answer off at second
  three, and it is why a follow-up "and tomorrow?" needs the phrase again.
- The **phrase is the barge-in signal**. While asleep, an onset sends nothing and cancels nothing;
  a detection sends `speech_start` and cancels the turn in flight, so "Hey Jarvis, stop" cuts an
  answer while room noise cannot.
- A **bare wake word** and a **cancel phrase** (`stop`, `quiet`, `cancel`, `hör auf`, `pare`, …)
  end the turn without touching the graph, and are excluded from `turn_timings`.
- A detector that raises **fails closed**: one `error` frame, then the socket closes with `1011`
  and the satellite reconnects with a fresh detector.

### Configuration

| Setting | Default | What it does |
|---|---|---|
| `VOICE_WAKE_WORD_ENABLED` | `false` | off = the old always-listening behaviour |
| `VOICE_WAKE_WORD_MODEL` | `hey_jarvis_v0.1.onnx` | bare filename in `models/openwakeword/` |
| `VOICE_WAKE_WORD_PHRASE` | `""` | what to strip from transcripts; empty derives it from the model |
| `VOICE_WAKE_WORD_THRESHOLD` | `0.5` | classifier probability that counts as "heard" |
| `VOICE_WAKE_WORD_DEBOUNCE_SECONDS` | `2.0` | one phrase = one detection |
| `VOICE_WAKE_WORD_WINDOW_SECONDS` | `6.0` | how long after the phrase its command may **start** |
| `VOICE_WAKE_WORD_ALLOW_OPT_OUT` | `true` | whether a client may send `wake_word: "off"` |

The client chooses with `wake_word`: `"auto"` (the satellite, by sending nothing), `"on"`,
`"off"` (refused by a strict server) or `"manual"` (the browser: a click is not ambient audio).

### What was measured, and what was not

Two tools produce the numbers, and they answer different questions.

**`scripts/wake_word_eval.py`** scores WAV files. Over the six committed fixtures at threshold
0.50: **0 false rejects, 0 false accepts**, 55× realtime. That is a plumbing check — the fixtures
are TTS, not a room.

**`scripts/make_wake_word_corpus.py`** builds a microphone-free corpus: six TTS voices, three
chatter clips, the question fixtures, each degraded offline (white noise at 40…5 dB SNR, low-pass
plus −12 dB for distance, synthetic reverb, resampling for pitch/tempo). Across all eight
conditions at threshold 0.50: **29/48 false rejects (60 %), 0/48 false accepts (0 %)**. Lowering
the threshold to 0.1 still leaves **31 %** false rejects while false accepts stay at 2 %.

That second result is not a tuning problem, and it is worth being blunt about why: **synthetic
speech is out of distribution for a model trained on human recordings.** If the operating point
were the cause, 0.1 would have rescued most of those clips; it did not. So:

- the **negative** side is validated — chatter, questions and noise never fired, at any threshold
  down to 0.2;
- the **positive** side is **UNVERIFIED**. Real recall needs either ten utterances recorded on the
  device, or ordinary daily use with the detections counted in the log. Do not quote the 60 %
  above as real-room recall; it is a statement about TTS.

**A 47-hour soak on the satellite** is the third source, and the only one that describes a room.
`scripts/soak_report.py` turns the three capture logs into one report (window, denominators and the
device-clock anchor included — see [`soak.md`](soak.md)). At threshold 0.50, over
`2026-11-09T18:59:45Z … 2026-11-11T17:45:00Z`:

| What | Value | Out of |
|---|---|---|
| wake word detections | 124 · score p50 **0.81**, p90 0.96 · 19 below 0.60 | 124 detections |
| turns accepted | 109 · **103 said in one breath** (`wake_lead` p50 −0.72 s) | 109 turns |
| segments rejected by the gate | **3 335** | 3 335 rejections |
| — never armed / >60 s / 10-60 s / 0.5-6 s / 6-10 s / ≤0.5 s | 2 352 / 792 / 141 / 31 / 15 / 4 | sums to 3 335 |
| room audio never transcribed | **2.3 h** (≈31 rejections per accepted turn) | 109 turns |
| turn failures, timeouts, detector failures | 0 / 0 / 0 | 109 turns |
| first audio heard | p50 **3.39 s**, p90 5.72 s | 102 turns with audio |

Read it this way:

- **The gate is working.** 3 335 segments never reached STT or the graph. That is 2.3 hours of
  speech not paid for, and it is the feature's whole point — it belongs next to the latencies, not
  next to the defects.
- **Ambient noise, not the permit, is what gets rejected.** Only 31 rejections sit in the
  `0.5-6 s` window that a "my second sentence was refused" report would produce, and ~1 % of all
  rejections land within 5 s of an accepted turn.
- **Precision and recall are still not measurable.** 124 detections is a reviewable set, but recall
  needs *near misses*, and nothing below 0.50 is logged. That is the next instrumentation step; until
  then the honest statement is "the gate rejects the room and accepts the phrase", not a percentage.

### Reading the log

| Line | What it answers |
|---|---|
| `wake word detected (score=…, in_turn=…)` | how often the phrase is heard, and how close to the threshold |
| `voice turn ignored: no wake word (speech=…, since_arm=…)` | your own pause exceeded the permit (`since_arm` sits just above the window on the pause path), or the permit was already consumed |
| `voice turn accepted (wake_lead=-0.42s)` | negative = one breath; positive = exactly how long you paused |
| `voice wake detector ready (realtime=…)` | CPU headroom; below ~5× is a warning |
| `voice session closed: <reason>` | exactly one per socket: `client gone`, `idle timeout`, or the exception class |

## Known limitations

- **The voice socket never reports the detected language**, so a spoken turn does not update
  the language badge in the UI. `_last_lang` is known when a turn finishes; the cheapest fix
  is one more field on the `audio_end` frame.
- **No client-side VAD and no offline queue**: no microphone means no session, and the turn
  is lost if the socket drops mid-answer (the transcript and reply are still in history).
- **An interrupted turn is recorded as an `[interrupted]` assistant message**, not as the
  partial reply the user actually heard. The session already sees every streamed token, so
  persisting the spoken prefix is possible; the marker is enough to keep the thread balanced
  and is far cheaper. The text channel has the same exposure through the `POST /chat`
  timeout — it abandons the run the same way, leaving the same unanswered `HumanMessage` —
  and has no repair yet.
- **The wake phrase is English only.** `de` and `pt-BR` conversations start with "Hey Jarvis";
  openWakeWord ships no German or Portuguese model. A custom model, or the on-device WakeNet10
  `wn10_de_halloesp` / `wn10_pt_olaesp` route, is the follow-up.
- **Wake-word recall on real human speech is UNVERIFIED** — see the measurement note above.
  While it stays unverified, treat the gate as "shipped but unproven" rather than done.
- **No diarization and no per-room barge-in tuning**, and `gpt-4o-mini-tts`'s voices are optimised
  for English — German and Portuguese are intelligible, not equal in quality.
- **TLS is deferred**: the browser is tested on localhost and the device uses plain `ws://`
  on the LAN. As a direct consequence, **the browser microphone does not work on a LAN
  address over HTTP** (secure-context rule, see "Browser microphone" above); typing still
  does.
- The browser client has not yet been validated end-to-end against a live backend; the
  socket contract itself is proven by `smoke_09`.
- **The first turn of a session has nothing to bias its language detection.** `_stt_bias()`
  returns `_last_lang`, which is empty until a turn has finished, so turn one sends no
  `language` at all and the provider auto-detects from a few seconds of audio. English
  containing a Portuguese cognate ("Brasil") in a Brazilian accent was decoded as Portuguese;
  because Whisper is one multilingual model, forcing a language produces a fluent translation
  rather than gibberish, so the transcript looked plausible and every later stage followed it.
  The misdetection then **sticks for the whole session**, since the next turn is hinted with
  it. Seeding the first turn's hint from the stored language preference — step 3 of
  `resolve_lang`'s chain, still a stub until M13 — closes it.
- **Chunk seams are audible.** Each chunk is a separate TTS request, so the reply has a
  prosodic seam wherever it was cut. Clause breaks turn that into a plausible pause, but the
  one after the opening chunk is still noticeable — see the latency section for the mechanism
  and the two tuning knobs (`VOICE_TTS_FIRST_CHUNK_MIN_CHARS` / `_MAX_CHARS`).
