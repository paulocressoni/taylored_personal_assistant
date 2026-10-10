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
| Spoken-answer prompt | `VOICE_STYLE_ADDENDUM` in `backend/app/prompts/responder.py` |
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
| start | `{"session_id": "...", "device_id": "...", "output_sample_rate": 16000 \| 24000}` | **Must be the first message.** Validated by `VoiceStartRequest`; a bad one is refused with `1003`. `device_id` and `output_sample_rate` are optional (`24000` default) |
| audio | binary PCM at `voice_input_sample_rate` (16 kHz) | Any frame size; the endpointer re-frames into 512-sample windows |
| stop | `{"type": "stop"}` | Ends the session; any turn in flight is cancelled. Any other `type` is logged and ignored |

### Server → client

| Frame | Content | Notes |
|---|---|---|
| `ready` | `{"input_sample_rate", "output_sample_rate", "format"}` | Once, after the start frame. **Audio sent before this is read as the start frame** and the socket is refused |
| `speech_start` | `{"type": "speech_start"}` | Every onset, including blips that never become a turn. Doubles as **"stop playing what you have buffered"** — this is the barge-in signal |
| `transcript` | `{"type": "transcript", "text": "..."}` | After STT. Not sent when the transcript is empty |
| `token` | `{"type": "token", "content": "..."}` | The responder's reply as text, token by token (the audio is the primary output) |
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
`HumanMessage` is durable — but the responder's `AIMessage` is only committed when that node
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
| Graph → first responder token | 300–600 ms |
| TTS first PCM byte | 200–400 ms |
| **Total** | **0.65–1.35 s** (target < 1.5 s) |

Measured with `scripts/voice_latency.py --runs 5 --warmup 1` (fixture `voice_en.wav`,
5 warm turns, 1 discarded):

| Stage | p50 | p90 |
|---|---|---|
| `stt_ms` | 288 ms | 501 ms |
| `graph_ttft_ms` | 2567 ms | 3008 ms |
| `tts_ttfb_ms` | 684 ms | 1387 ms |
| ↳ `generation_ms` (first→last token) | 98 ms | 133 ms |
| ↳ `tts_call_ms` (last token→first audio) | 551 ms | 1290 ms |
| **`first_audio_ms`** | **3602 ms** | **4242 ms** |
| `perceived_ms` (from the caller's last frame) | 3888 ms | 4528 ms |

Read this honestly:

- **STT meets its budget.** The 1.2 s seen in the very first cold run was TCP/TLS
  handshake, not transcription — which is exactly why the tool discards a warm-up run.
- **The graph is ~71% of the wait.** `graph_ttft_ms` covers **three sequential model
  round-trips** (router → specialist → responder); the budget line above assumes one.
- **A one-sentence answer cannot be pipelined.** The sentence splitter only fires on a
  terminator that reaches `VOICE_TTS_MIN_CHUNK_CHARS`, so for `The capital of France is
  Paris.` the first TTS request is sent only after the reply is complete —
  `tts_call_ms` is the real TTS round trip, and `tts_ttfb_ms` is mostly "waiting for the
  reply". Multi-sentence replies do pipeline.
- **`perceived_ms` is generous.** It starts from the last frame the client sent, and the
  fixture's own tail is already silent, so it understates the human wait by ~230 ms. With
  a live microphone the caller also feels the full 500 ms silence window, so the honest
  figure today is `first_audio_ms` + `VOICE_VAD_MIN_SILENCE_MS` ≈ 4.1 s.
- **`1.5 s` is not reachable with a three-hop graph and a ~550 ms TTS round trip.** That
  is a decision to take, not a bug to fix: either the graph gets a voice fast path, or this
  page publishes a budget it actually meets.

Levers, roughly in order of value: a faster TTS provider (both vendors sit behind the
`Synthesizer` Protocol — a config switch plus one adapter), a voice fast path in the graph,
`VOICE_VAD_MIN_SILENCE_MS` 500 → 300 (a straight −200 ms of felt latency), and
`VOICE_TTS_MIN_CHUNK_CHARS` (only helps multi-sentence replies, where it lets a short
opening sentence start sooner).

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
| `VOICE_TTS_MIN_CHUNK_CHARS` / `_MAX_CHUNK_CHARS` | `40` / `240` | How the reply is cut into speakable chunks (and the reason a one-sentence reply cannot pipeline) |
| `VOICE_INPUT_SAMPLE_RATE` | `16000` | Pinned to the VAD window by a config validator |
| `VOICE_OUTPUT_SAMPLE_RATE` | `24000` | Browser default; a device asks for 16000 in its start frame |
| `VOICE_VAD_*` | `0.5` / `0.35` / `150` / `30` / `500` / `30` | Threshold, negative threshold, min speech, speech pad, min silence, max utterance |
| `VOICE_IDLE_TIMEOUT_SECONDS` | `60` | A socket that sends neither audio nor a keepalive for this long is dropped, releasing its slot |
| `VOICE_MAX_SESSIONS` | `3` | Concurrent sessions; each holds a provider quota and its own ONNX session |

## Tooling

```powershell
cd backend

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

`smoke_08` drives the **real** `VoiceSession` (real VAD, real providers, real resampler)
and writes the reply as a WAV, so the only thing left to explain a slow turn is the
pipeline. `voice_latency` builds the providers and the graph **once** — that is what keeps
the TLS connections warm — and reports p50/p90 per stage, with `_TimedGraph` splitting
`graph_ttft_ms` into per-node model time and the recorder splitting `tts_ttfb_ms` into
generation and the TTS round trip. Both are integration-only: they cost money and need
network.

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

The Python simulator already speaks this contract; the firmware does not exist yet
(`firmware/` is empty). What the board requires:

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
- **No wake word, no diarization, no barge-in tuning per room**, and `gpt-4o-mini-tts`'s
  voices are optimised for English — German and Portuguese are intelligible, not equal in
  quality.
- **TLS is deferred**: the browser is tested on localhost and the device uses plain `ws://`
  on the LAN.
- The browser client has not yet been validated end-to-end against a live backend; the
  socket contract itself is proven by `smoke_09`.
