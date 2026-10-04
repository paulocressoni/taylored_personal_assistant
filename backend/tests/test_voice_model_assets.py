"""The vendored Silero VAD graph must ship with the package and keep its contract.

The graph is a committed binary, so nothing else in the suite would notice a
corrupt checkout, a wheel that dropped the asset, or an upstream contract
change. Its dimensions are symbolic, so the concrete window and state widths
that callers must supply are asserted here instead.
"""

from pathlib import Path

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
