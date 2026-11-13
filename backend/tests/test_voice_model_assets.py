"""The vendored Silero VAD graph must ship with the package and keep its contract.

The graph is a committed binary, so nothing else in the suite would notice a
corrupt checkout, a wheel that dropped the asset, or an upstream contract
change. Its dimensions are symbolic, so the concrete window and state widths
that callers must supply are asserted here instead.
"""

import hashlib
from pathlib import Path
from typing import Any

import numpy as np
import onnxruntime as ort
import pytest

import app.voice

MODEL_PATH = Path(app.voice.__file__).resolve().parent / "models" / "silero_vad.onnx"

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512
CONTEXT_SAMPLES = 64
STATE_SHAPE = (2, 1, 128)
SR = np.array(SAMPLE_RATE, dtype=np.int64)


@pytest.fixture(scope="module")
def session() -> ort.InferenceSession:
    """Load the vendored graph once for the whole module."""
    return ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])


def _step(
    session: ort.InferenceSession,
    samples: np.ndarray,
    state: np.ndarray,
    context: np.ndarray,
) -> tuple[float, np.ndarray, np.ndarray]:
    """Score one window and carry the context and recurrent state forward.

    The graph declares symbolic dimensions, so a wrong window width is not
    rejected at the call boundary — it has to be correct here.

    Args:
        session: Loaded ONNX Runtime session.
        samples: `WINDOW_SAMPLES` new samples at 16 kHz.
        state: Recurrent state from the previous call, shaped `STATE_SHAPE`.
        context: Trailing `CONTEXT_SAMPLES` samples of the previous window.

    Returns:
        A `(probability, state, context)` triple for the next call.
    """
    window = np.concatenate(
        [context, samples.reshape(1, WINDOW_SAMPLES)], axis=1
    ).astype(np.float32)
    probabilities, next_state = session.run(
        ["output", "stateN"],
        {"input": window, "state": state, "sr": SR},
    )
    return (
        float(probabilities[0, 0]),
        next_state.reshape(STATE_SHAPE),
        window[:, -CONTEXT_SAMPLES:],
    )


def test_model_file_is_present() -> None:
    assert MODEL_PATH.is_file()
    assert MODEL_PATH.stat().st_size > 1_000_000


def test_model_declares_the_documented_io_contract(
    session: ort.InferenceSession,
) -> None:
    inputs = {node.name: node for node in session.get_inputs()}
    outputs = {node.name: node for node in session.get_outputs()}

    assert set(inputs) == {"input", "state", "sr"}
    assert set(outputs) == {"output", "stateN"}

    assert inputs["input"].type == "tensor(float)"
    assert inputs["state"].type == "tensor(float)"
    assert inputs["state"].shape[0] == 2
    assert inputs["state"].shape[1] is None, "state batch dim should stay dynamic"
    assert inputs["state"].shape[2] == 128

    # `sr` is rank-0, so callers must pass a 0-d array rather than a 1-element one.
    assert inputs["sr"].type == "tensor(int64)"
    assert inputs["sr"].shape == []


def test_model_scores_silence_below_threshold(session: ort.InferenceSession) -> None:
    state = np.zeros(STATE_SHAPE, dtype=np.float32)
    context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    probabilities = []
    for _ in range(4):
        probability, state, context = _step(
            session, np.zeros(WINDOW_SAMPLES, dtype=np.float32), state, context
        )
        probabilities.append(probability)

    assert state.shape == STATE_SHAPE
    assert context.shape == (1, CONTEXT_SAMPLES)
    assert all(0.0 <= probability <= 1.0 for probability in probabilities)
    assert max(probabilities) < 0.5


# --- wake word graphs (openWakeWord) ----------------------------------------
#
# These are committed binaries with a symbolic-dimension contract, so the same
# reasoning as the VAD graph applies: nothing else in the suite would notice a
# corrupt checkout, a wheel that dropped them, or an upstream change. The pins
# are read from the downloader so there is one source of truth for the bytes.

WAKE_DIR = Path(app.voice.__file__).resolve().parent / "models" / "openwakeword"
WAKE_FIXTURES = Path(__file__).resolve().parent / "fixtures"
ANALYSIS_SAMPLES = 12_640
ANALYSIS_BYTES = ANALYSIS_SAMPLES * 2
FEATURE_ROWS = 16
FEATURE_WIDTH = 96


@pytest.fixture(scope="module")
def wake_models() -> Any:
    """Load the three wake graphs once for the whole module."""
    from app.voice.wake import OpenWakeWordModels

    return OpenWakeWordModels(model_dir=WAKE_DIR)


def _wake_scores(models: Any, name: str) -> list[float]:
    """Score one fixture with the real detector and return the raw hop scores."""
    from app.voice.audio import read_wav
    from app.voice.wake import OpenWakeWordDetector

    pcm, rate, _ = read_wav(WAKE_FIXTURES / name)
    assert rate == SAMPLE_RATE
    detector = OpenWakeWordDetector(
        models, threshold=0.5, debounce_s=0.0, seed=models.silence_embedding
    )
    return detector.push_scores(pcm)


def test_wake_graphs_match_their_pinned_size_and_hash() -> None:
    from scripts.download_wake_word_models import ASSETS

    for asset in ASSETS:
        path = WAKE_DIR / asset.name
        assert path.is_file(), f"{asset.name} is missing from the package"
        data = path.read_bytes()
        assert len(data) == asset.size
        assert hashlib.sha256(data).hexdigest() == asset.sha256


def test_mel_graph_turns_one_analysis_window_into_76_frames(
    wake_models: Any,
) -> None:
    # The frame count is the whole arithmetic: 12 640 samples -> 76 frames, which
    # is what the embedding graph's fixed 76-row window requires.
    window = wake_models.embed(bytes(ANALYSIS_BYTES))
    assert window.shape == (FEATURE_WIDTH,)
    assert np.isfinite(window).all()


def test_embedding_width_matches_the_classifier_contract(wake_models: Any) -> None:
    rows = np.zeros((FEATURE_ROWS, FEATURE_WIDTH), dtype=np.float32)
    score = wake_models.classify(rows)
    assert 0.0 <= score <= 1.0


def test_classify_rejects_a_history_of_the_wrong_shape(wake_models: Any) -> None:
    with pytest.raises(ValueError, match="16, 96"):
        wake_models.classify(np.zeros((15, FEATURE_WIDTH), dtype=np.float32))


def test_embed_rejects_a_short_window(wake_models: Any) -> None:
    with pytest.raises(ValueError, match=str(ANALYSIS_BYTES)):
        wake_models.embed(bytes(ANALYSIS_BYTES - 2))


def test_the_silence_seed_is_finite_and_scores_near_zero(wake_models: Any) -> None:
    seed = wake_models.silence_embedding
    assert seed.shape == (FEATURE_WIDTH,)
    assert np.isfinite(seed).all()

    # A history made only of silence must never look like a wake word; this is
    # what makes seeding safe instead of an invented detection.
    rows = np.tile(seed, (FEATURE_ROWS, 1))
    assert wake_models.classify(rows) < 0.05


def test_the_committed_wake_fixture_crosses_the_threshold(wake_models: Any) -> None:
    scores = _wake_scores(wake_models, "wake_en.wav")
    assert max(scores) >= 0.5, f"positive control scored {max(scores):.3f}"


def test_an_ordinary_question_never_triggers_the_wake_word(
    wake_models: Any,
) -> None:
    scores = _wake_scores(wake_models, "voice_en.wav")
    assert max(scores) < 0.1, f"negative control scored {max(scores):.3f}"


def test_the_mel_graph_needs_int16_amplitude_not_normalised_samples() -> None:
    # The trap this module exists to prevent: `to_float32` returns [-1, 1), the
    # graph expects int16 amplitude, and the difference is a ~90 dB offset that
    # silently collapses every score to zero. Pin it with the same audio twice.
    from app.voice.audio import read_wav
    from app.voice.wake import OpenWakeWordModels

    pcm, _, _ = read_wav(WAKE_FIXTURES / "wake_en.wav")
    raw = np.frombuffer(pcm[:ANALYSIS_BYTES], dtype="<i2")

    session = ort.InferenceSession(
        str(WAKE_DIR / "melspectrogram.onnx"), providers=["CPUExecutionProvider"]
    )
    scaled = np.squeeze(
        session.run(None, {"input": raw.astype(np.float32)[None, :]})[0]
    )
    normalised = np.squeeze(
        session.run(None, {"input": (raw / 32768.0).astype(np.float32)[None, :]})[0]
    )

    assert scaled.shape == (76, 32)
    assert scaled.mean() - normalised.mean() > 50.0
    assert OpenWakeWordModels(model_dir=WAKE_DIR).embed(pcm[:ANALYSIS_BYTES]).shape == (
        FEATURE_WIDTH,
    )
