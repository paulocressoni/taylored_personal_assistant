# Vendored wake-word models — openWakeWord (`hey_jarvis_v0.1`)

Model weights are committed on purpose, exactly like the Silero VAD graph one
directory up: the runtime image is a two-stage `python:3.12-slim` build with no apt
packages and no network access at build time, so a build-time download would be
both fragile and a supply-chain hole.

## Provenance

| File | Bytes | SHA256 |
|---|---|---|
| `melspectrogram.onnx` | 1 087 958 | `ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f` |
| `embedding_model.onnx` | 1 326 578 | `70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f` |
| `hey_jarvis_v0.1.onnx` | 1 271 370 | `94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb` |

| Field | Value |
|---|---|
| Project | openWakeWord — <https://github.com/dscripka/openWakeWord> |
| Release | `v0.5.1` (the assets openWakeWord `0.6.0`, published 2024-02-11, downloads) |
| Downloaded from | `https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/<file>` |
| Verified with | `backend/scripts/download_wake_word_models.py` (`--check` mode) |

The SHA256 column is the authoritative pin: it is what the committed bytes
actually are, independent of the release tag. A change to any of them means the
behaviour of the assistant changed, so the downloader refuses to write a file
that does not match.

## Licence — the code is Apache-2.0, these models are not

Upstream's README, §License:

> All of the code in this repository is licensed under the **Apache 2.0** license.
> All of the included pre-trained models are licensed under the
> [Creative Commons Attribution-NonCommercial-ShareAlike 4.0 International](https://creativecommons.org/licenses/by-nc-sa/4.0/)
> license due to the inclusion of datasets with unknown or restrictive licensing
> as part of the training data.

What that means here:

- **Attribution** — this file and [`LICENSE`](LICENSE) are the attribution. They must travel with the binaries,
  which is why `pyproject.toml` ships this file next to the `.onnx` files, not only
  in the git tree.
- **NonCommercial** — fine for a self-hosted home assistant; it forbids selling the
  appliance or running a paid service on these models.
- **ShareAlike** — applies to *adapted* models. These are redistributed unmodified.
- The embedding graph re-implements Google's `speech_embedding` module (Apache-2.0),
  which is where the 96-float feature layout comes from.

If permissive licensing ever becomes a requirement, the exit is a different engine
(Vosk keyword spotting) or an on-device WakeNet model, not a fork of these weights.

## Why these files and not the PyPI package

`pip install openwakeword` cannot be resolved on this project: it requires
`tflite-runtime` on Linux, and `tflite-runtime` publishes no cp312/cp313 wheels
(newest is cp311) while the backend requires `>=3.12,<3.14`. The package would also
add `scipy`, `scikit-learn`, `tqdm` and `requests` for code paths a streaming
service never uses. We drive the ONNX graphs with the `onnxruntime` we already
depend on, mirroring `app/voice/vad.py` and its Silero graph.

## I/O contract (16 kHz, streaming)

The ONNX graphs declare **symbolic** dimensions, so ONNX Runtime does not validate
the widths that matter. "Declared" is what `session.get_inputs()` reports;
"Effective" is what a correct caller must pass. The numbers below were measured by
running the graphs, not copied from documentation.

| Graph | Tensor | Dir | Type | Declared | Effective |
|---|---|---|---|---|---|
| melspectrogram | `input` | in | float32 | `['batch_size', 'samples']` | `[1, 12 640]` |
| melspectrogram | `output` | out | float32 | `['time', 1, 'Clipoutput_dim_2', 32]` | `[1, 1, 76, 32]` |
| embedding | `input_1` | in | float32 | `['unk__314', 76, 32, 1]` | `[1, 76, 32, 1]` |
| embedding | `conv2d_19` | out | float32 | `['unk__315', 1, 1, 96]` | `[1, 1, 1, 96]` |
| hey_jarvis | `x.1` | in | float32 | `[1, 16, 96]` | `[1, 16, 96]` |
| hey_jarvis | `53` | out | float32 | `[1, 1]` | `[1, 1]` |

Frame arithmetic, measured: **12 640 samples (0.79 s) → exactly 76 frames**;
12 160 samples → 73. The window is 76 mel frames at a 10 ms hop plus one 30 ms
analysis window, which is why the constant is `76 × 160 + 480`.

## Caller obligations

None of these is enforced by the runtime; each one fails silently or with an
opaque graph error if broken.

1. **Samples are `float32` carrying int16 amplitude** (±32768), *not* the
   `[-1, 1)` form `app.voice.audio.to_float32` returns. Measured on a real
   recording: mel mean `58.55` int16-scale versus `-31.76` normalised — a 90-unit
   offset that collapses every wake score to zero.
2. The mel output must be transformed with **`x/10 + 2`** before the embedding
   graph, as upstream does.
3. Feed **exactly 12 640 samples** per call and take the resulting `(76, 32)`
   frames; one new embedding per **1 280-sample (80 ms) hop**.
4. The classifier always receives the last **16 embeddings** as `(1, 16, 96)`.
5. Set `intra_op_num_threads = inter_op_num_threads = 1`. The graphs are tiny and
   the default pool oversubscribes once a second voice socket opens.

`OpenWakeWordDetector` seeds its 16-row history with the embedding of a silent
window (computed once at load), so the first real analysis window can already be
classified — and silence can never invent a detection. Measured: a seed-only
history scores `3.9e-05`, random embeddings `8.3e-05`.

## Measured cost

Single-threaded, one core, this host: mel `0.53 ms` + embedding `1.33 ms` +
classifier `0.04 ms` = **1.89 ms per 80 ms hop → 42× realtime → 2.4 % of a core per
session** (≈7.1 % with three concurrent detectors).

## Updating

1. Pick the new release tag and update `RELEASE_TAG`, `ASSETS` (name, size,
   SHA256) in `backend/scripts/download_wake_word_models.py`.
2. Delete the old files and re-run the script.
3. Run `pytest tests/test_voice_model_assets.py` — a failure means the I/O contract
   changed and the table above needs updating.
4. Update this table and note the bump in `CHANGELOG.md` if it is user-visible.
5. If the model name changes, update `VOICE_WAKE_WORD_MODEL` (or its default) and
   `PHRASES` in `backend/app/voice/wake.py`, and re-measure the positive control.
