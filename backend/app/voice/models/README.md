# Vendored VAD model — Silero VAD (`silero_vad.onnx`)

Model weights are committed on purpose. The runtime image is a two-stage
`python:3.12-slim` build with no apt packages and no network access at build
time, so a build-time download would be both fragile and a supply-chain hole.

## Provenance

| Field | Value |
|---|---|
| Project | Silero VAD — <https://github.com/snakers4/silero-vad> |
| Release | `v6.2.3` ("Make torchaudio optional") |
| Tag commit | `5cd7945676eb32225748052e2e6a0580e4686a08` |
| File in repo | `src/silero_vad/data/silero_vad.onnx` |
| Downloaded from | `https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.3/src/silero_vad/data/silero_vad.onnx` |
| Size | 2 327 524 bytes |
| SHA256 | `<SHA256_FROM_GET-FILEHASH>` |
| Licence | MIT — Copyright (c) 2020-present Silero Team |

The SHA256 above is the authoritative pin: it is what the vendored bytes
actually are, independent of the tag. Re-verify after any refresh with
`Get-FileHash app/voice/models/silero_vad.onnx -Algorithm SHA256`.

## Why this file and not the PyPI package

We drive the ONNX graph with `onnxruntime` directly. The `silero-vad` PyPI
package would additionally install **torch** for a model that does not need it,
adding hundreds of megabytes to the image for no benefit.

We also do not use `silero_vad_16k_sequence.onnx` (added in v6.2.2). That graph
is built for offline batch runs; endpointing needs the streaming, stateful graph
so a decision can be made after any single window.

## I/O contract (16 kHz, streaming)

The graph declares **symbolic** dimensions — batching was added upstream in v4 —
so ONNX Runtime does not validate the two widths we care about. The "Declared"
column is what `session.get_inputs()` reports; "Effective" is what a correct
caller must pass. `backend/tests/test_voice_model_assets.py` pins both.

| Tensor | Dir | Type | Declared | Effective | Meaning |
|---|---|---|---|---|---|
| `input` | in | float32 | `[None, None]` | `[1, 576]` | 64 context samples prepended to 512 new samples |
| `state` | in | float32 | `[2, None, 128]` | `[2, 1, 128]` | recurrent state, carried across calls |
| `sr` | in | int64 | `[]` | rank-0 `16000` | sample rate as a scalar, not a 1-element array |
| `output` | out | float32 | `[None, 1]` | `[1, 1]` | speech probability |
| `stateN` | out | float32 | `[None, None, None]` | `[2, 1, 128]` | state for the next call |

Caller obligations (none of which the graph enforces):

1. Send exactly **576** samples per call: the last **64** samples of the previous
   window, followed by **512** new ones. A 512-wide input is accepted by the
   runtime and fails later with an opaque error inside the graph.
2. Chain `stateN` back into `state`, reshaping to `(2, 1, 128)` so a batch
   mismatch fails immediately instead of silently.
3. Zero both the state and the context at the start of every new stream, or a
   fresh session inherits the previous one's memory.

## Updating

1. Pick the new tag and download it into this directory (overwrite in place).
2. Re-run `pytest backend/tests/test_voice_model_assets.py` — a failure there
   means the contract changed and this table needs updating.
3. Update the release, tag commit, size, URL and SHA256 rows above.
4. Note the bump in `CHANGELOG.md` if the change is user-visible.
