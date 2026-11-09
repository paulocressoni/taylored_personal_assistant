"""Unit tests for the wake-word detector, its gate and the text helpers.

Deliberately ONNX-free: the scorer is scripted, so these tests exercise the parts
that can be wrong in a way no model would reveal — hop re-framing, warm-up, the
one-shot permit, debounce and phrase stripping. The graphs' own contract is pinned
in `test_voice_model_assets.py`.
"""

import numpy as np
import pytest

from app.voice.wake import (
    ANALYSIS_BYTES,
    FEATURE_WIDTH,
    HOP_BYTES,
    Detection,
    OpenWakeWordDetector,
    WakeGate,
    is_cancel_phrase,
    resolve_wake_phrase,
    strip_wake_phrase,
)

SEED = np.zeros(FEATURE_WIDTH, dtype=np.float32)


class ScriptedScorer:
    """Duck-typed `WakeScorer` returning scripted scores, one per classification.

    Records the window lengths it was handed: the re-framing rules are exactly
    "how much audio reached `embed`, and how often", which is invisible if the
    fake only returns numbers.
    """

    def __init__(self, *scores: float) -> None:
        self._scores = list(scores)
        self.windows: list[int] = []
        self.rows: list[tuple[int, ...]] = []

    def embed(self, window: bytes) -> np.ndarray:
        """Record the window size and return a distinguishable vector."""
        self.windows.append(len(window))
        return np.full(FEATURE_WIDTH, float(len(self.windows)), dtype=np.float32)

    def classify(self, rows: np.ndarray) -> float:
        """Record the history shape and return the next scripted score."""
        self.rows.append(rows.shape)
        assert rows.shape == (16, FEATURE_WIDTH)
        return self._scores.pop(0) if self._scores else 0.0


def _detector(
    scorer: ScriptedScorer,
    *,
    threshold: float = 0.5,
    debounce_s: float = 0.0,
    clock: list[float] | None = None,
) -> OpenWakeWordDetector:
    """Build a seeded detector whose clock the test controls."""
    now = (lambda: clock[0]) if clock is not None else None
    kwargs = {"threshold": threshold, "debounce_s": debounce_s, "seed": SEED}
    if now is not None:
        kwargs["now"] = now
    return OpenWakeWordDetector(scorer, **kwargs)


# --- re-framing and warm-up -------------------------------------------------

# The window is 25 280 bytes, a hop is 2 560, so NINE hops leave the window short
# and the tenth fills it. Priming with nine hops therefore makes every following
# hop produce exactly one score, which is what keeps these tests about re-framing
# rather than about arithmetic.
PRIMING_HOPS = 9
HOP = b"\x00" * HOP_BYTES


def _prime(detector: OpenWakeWordDetector, hops: int = PRIMING_HOPS) -> list[float]:
    """Feed hops that leave the analysis window short, returning any scores.

    Not asserting inside on purpose: a test that primes after a partial hop needs
    a different count, and a helper that asserts would blame the helper.
    """
    scores: list[float] = []
    for _ in range(hops):
        scores.extend(detector.push_scores(HOP))
    return scores


def test_nothing_is_embedded_before_the_analysis_window_is_full() -> None:
    scorer = ScriptedScorer()
    detector = _detector(scorer)

    assert _prime(detector) == []
    assert scorer.windows == []
    assert scorer.rows == []


def test_the_tenth_hop_embeds_exactly_one_analysis_window() -> None:
    scorer = ScriptedScorer()
    detector = _detector(scorer)

    _prime(detector)
    detector.push_scores(HOP)

    assert scorer.windows == [ANALYSIS_BYTES]
    assert scorer.rows == [(16, FEATURE_WIDTH)]


def test_audio_is_reframed_from_arbitrary_chunk_sizes() -> None:
    scorer = ScriptedScorer()
    detector = _detector(scorer)
    _prime(detector)

    # 2.5 hops, delivered 100 bytes at a time: exactly two hops complete, and
    # every window handed to the embedder is the full analysis size.
    payload = b"\x00" * (2 * HOP_BYTES + HOP_BYTES // 2)
    for offset in range(0, len(payload), 100):
        detector.push_scores(payload[offset : offset + 100])

    assert len(scorer.windows) == 2
    assert set(scorer.windows) == {ANALYSIS_BYTES}


def test_no_sample_is_lost_across_uneven_chunks() -> None:
    scorer = ScriptedScorer()
    detector = _detector(scorer)

    # From a cold start, ten hops fill the window. One byte short of that, nothing
    # is embedded; the missing byte completes the tenth hop and the window.
    detector.push_scores(b"\x00" * (10 * HOP_BYTES - 1))
    assert scorer.windows == []

    detector.push_scores(b"\x00")
    assert len(scorer.windows) == 1
    assert scorer.windows[0] == ANALYSIS_BYTES


def test_an_unseeded_detector_waits_for_sixteen_embeddings() -> None:
    scorer = ScriptedScorer(*[1.0] * 40)
    detector = OpenWakeWordDetector(scorer, threshold=0.5, debounce_s=0.0)

    # The first embedding lands on hop 10, so 24 hops yield 15 of them: one
    # short of the classifier's window, and nothing may be classified yet.
    detector.push_scores(HOP * 24)
    assert len(scorer.windows) == 15
    assert scorer.rows == []

    detector.push_scores(HOP)
    assert scorer.rows == [(16, FEATURE_WIDTH)]


# --- threshold and debounce -------------------------------------------------


def test_a_score_at_the_threshold_is_a_detection() -> None:
    detector = _detector(ScriptedScorer(0.5), threshold=0.5)
    _prime(detector)

    detections = detector.push(HOP)
    assert [round(d.score, 2) for d in detections] == [0.5]


def test_push_reports_every_crossing_in_one_chunk() -> None:
    detector = _detector(ScriptedScorer(0.9, 0.8, 0.1), debounce_s=0.0)
    _prime(detector)

    detections = detector.push(HOP * 3)
    assert isinstance(detections[0], Detection)
    assert [round(d.score, 2) for d in detections] == [0.9, 0.8]


def test_debounce_suppresses_a_repeat_until_the_clock_moves_on() -> None:
    clock = [100.0]
    detector = _detector(ScriptedScorer(0.9, 0.9, 0.9), debounce_s=2.0, clock=clock)
    _prime(detector)

    clock[0] = 100.0
    assert len(detector.push(HOP)) == 1

    clock[0] = 101.0
    assert detector.push(HOP) == []

    clock[0] = 103.0
    assert len(detector.push(HOP)) == 1


def test_push_scores_ignores_the_threshold() -> None:
    detector = _detector(ScriptedScorer(0.01, 0.02), threshold=0.5)
    _prime(detector)

    assert detector.push_scores(HOP * 2) == [0.01, 0.02]


def test_reset_drops_leftover_audio_and_the_debounce_state() -> None:
    clock = [0.0]
    detector = _detector(ScriptedScorer(0.9, 0.9), debounce_s=60.0, clock=clock)
    # Ten hops fill the window and produce the first detection, which starts the
    # 60 s debounce.
    assert len(detector.push(HOP * 10)) == 1

    detector.reset()

    # A partial hop from before the reset must not complete a hop now.
    assert detector.push_scores(b"\x00" * (HOP_BYTES - 1)) == []
    assert detector.push_scores(b"\x00" * 1) == []
    # Nine more hops reach the window again; the debounce state is gone, so the
    # crossing is reported even though the clock barely moved.
    clock[0] = 0.5
    assert len(detector.push(HOP * 9)) == 1


def test_a_seed_of_the_wrong_width_is_rejected() -> None:
    with pytest.raises(ValueError, match="96"):
        OpenWakeWordDetector(
            ScriptedScorer(), threshold=0.5, debounce_s=0.0, seed=np.zeros(10)
        )


# --- the gate ---------------------------------------------------------------


def test_the_gate_is_closed_until_armed() -> None:
    clock = [0.0]
    gate = WakeGate(6.0, now=lambda: clock[0])

    assert gate.is_open() is False
    assert gate.admit(0.0) is False
    assert gate.last_arm_at is None


def test_admitting_a_segment_consumes_the_permit() -> None:
    clock = [10.0]
    gate = WakeGate(6.0, now=lambda: clock[0])
    gate.arm()

    assert gate.admit(10.5) is True
    assert gate.admit(10.6) is False
    assert gate.is_open() is False
    assert gate.remaining() == 0.0


def test_a_segment_that_started_before_the_arm_is_admitted() -> None:
    clock = [10.0]
    gate = WakeGate(6.0, now=lambda: clock[0])
    clock[0] = 10.4
    gate.arm()

    # The one-breath path: the phrase's own segment began before the detection.
    assert gate.admit(9.8) is True


def test_a_segment_that_starts_after_the_window_is_refused() -> None:
    clock = [0.0]
    gate = WakeGate(6.0, now=lambda: clock[0])
    gate.arm()

    clock[0] = 7.0
    assert gate.is_open() is False
    assert gate.admit(7.0) is False


def test_rearming_replaces_the_previous_deadline() -> None:
    clock = [0.0]
    gate = WakeGate(6.0, now=lambda: clock[0])
    gate.arm()

    clock[0] = 5.0
    gate.arm()

    clock[0] = 10.0
    assert gate.is_open() is True
    assert gate.last_arm_at == 5.0
    assert gate.remaining() == pytest.approx(1.0)


# --- text helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hey Jarvis, what's the weather?", "what's the weather?"),
        ("Hey, Jarvis! Turn it up.", "Turn it up."),
        ("  Hey   Jarvis   what time is it  ", "what time is it"),
        ("Hey Jarvis", ""),
        ("Hey Jarvis.", ""),
        # A phrase that is not leading is left alone: the answer still works.
        ("Jarvis, what time is it?", "Jarvis, what time is it?"),
        ("What time is it?", "What time is it?"),
    ],
)
def test_strip_wake_phrase_removes_only_a_leading_phrase(
    text: str, expected: str
) -> None:
    assert strip_wake_phrase(text, "hey jarvis") == expected


def test_strip_wake_phrase_accepts_a_configured_phrase_with_punctuation() -> None:
    assert strip_wake_phrase("Hey Jarvis, wie ist das Wetter?", "Hey, Jarvis!") == (
        "wie ist das Wetter?"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Stop!", True),
        ("stop it", True),
        ("  Stopp  ", True),
        ("Hör auf!", True),
        ("silêncio", True),
        # Not an exact phrase: these are real questions and must be answered.
        ("stop the music", False),
        ("Stop it now please", False),
        ("", False),
    ],
)
def test_is_cancel_phrase_matches_exact_normalised_phrases(
    text: str, expected: bool
) -> None:
    assert is_cancel_phrase(text) is expected


def test_resolve_wake_phrase_derives_the_builtin_phrase() -> None:
    assert resolve_wake_phrase("hey_jarvis_v0.1.onnx", "") == "hey jarvis"


@pytest.mark.parametrize("configured", ["hey jarvis", "Hey Jarvis!", "  hey, jarvis  "])
def test_resolve_wake_phrase_accepts_the_same_phrase_spelled_differently(
    configured: str,
) -> None:
    assert resolve_wake_phrase("hey_jarvis_v0.1.onnx", configured) == configured.strip()


def test_resolve_wake_phrase_allows_any_phrase_for_a_custom_model() -> None:
    assert resolve_wake_phrase("my_house_v0.1.onnx", "okay house") == "okay house"


def test_resolve_wake_phrase_rejects_an_empty_phrase_for_a_custom_model() -> None:
    with pytest.raises(RuntimeError, match="has no built-in phrase"):
        resolve_wake_phrase("my_house_v0.1.onnx", "   ")


def test_resolve_wake_phrase_rejects_a_phrase_the_model_cannot_hear() -> None:
    with pytest.raises(RuntimeError, match="the phrase must match the model"):
        resolve_wake_phrase("hey_jarvis_v0.1.onnx", "ok google")
