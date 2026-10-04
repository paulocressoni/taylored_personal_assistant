"""Unit tests for the VAD wrapper and the endpointing state machine."""

import pytest

from app.voice.vad import (
    WINDOW_BYTES,
    WINDOW_SAMPLES,
    EndpointDetector,
    Ignored,
    SileroVad,
    SpeechStart,
    Utterance,
    Vad,
)

PAD_BYTES = 960  # 30 ms of 16 kHz mono s16le


class ScriptedVad:
    """Deterministic VAD stub that returns one scripted probability per window."""

    def __init__(self, *probabilities: float) -> None:
        self._probabilities = list(probabilities)
        self.windows: list[bytes] = []
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def speech_probability(self, window: bytes) -> float:
        self.windows.append(window)
        if not self._probabilities:
            raise AssertionError("ScriptedVad ran out of scripted probabilities")
        return self._probabilities.pop(0)


def _window(fill: int = 0) -> bytes:
    """Build a window whose bytes are all `fill`, so provenance is checkable."""
    return bytes([fill]) * WINDOW_BYTES


def _detector(vad: Vad, **overrides: float) -> EndpointDetector:
    """Build a detector with the shipped defaults, overridable per test."""
    defaults: dict[str, float] = {
        "threshold": 0.5,
        "neg_threshold": 0.35,
        "min_speech_ms": 150.0,
        "min_silence_ms": 500.0,
        "speech_pad_ms": 30.0,
        "max_utterance_s": 30.0,
    }
    defaults.update(overrides)
    return EndpointDetector(vad, **defaults)


# --- re-framing -------------------------------------------------------------


def test_silence_produces_no_events() -> None:
    detector = _detector(ScriptedVad(*[0.0] * 40))
    assert detector.push(_window() * 40) == []


def test_audio_is_reframed_from_arbitrary_chunk_sizes() -> None:
    vad = ScriptedVad(*[0.0] * 4)
    detector = _detector(vad)

    # 960 bytes cannot fill a 1024-byte window, so nothing is scored yet.
    assert detector.push(bytes(960)) == []
    assert vad.windows == []

    detector.push(bytes(64))
    assert len(vad.windows) == 1

    detector.push(bytes(2048))
    assert len(vad.windows) == 3


def test_no_samples_are_lost_across_uneven_chunks() -> None:
    vad = ScriptedVad(*[0.0] * 40)
    detector = _detector(vad)

    for _ in range(10):
        detector.push(bytes(320))  # 3200 bytes = 3 windows + 128 held back
    assert len(vad.windows) == 3

    detector.push(bytes(320))
    assert len(vad.windows) == 3

    for _ in range(2):
        detector.push(bytes(320))  # 4160 bytes = 4 windows + 64 held back
    assert len(vad.windows) == 4


# --- turn boundaries --------------------------------------------------------


def test_onset_emits_speech_start_on_the_first_loud_window() -> None:
    vad = ScriptedVad(0.9, *[0.0] * 20)
    detector = _detector(vad)

    assert detector.push(_window(1)) == [SpeechStart()]
    assert len(vad.windows) == 1


def test_a_confirmed_turn_emits_utterance_trimmed_to_the_pad() -> None:
    vad = ScriptedVad(*[0.9] * 10, *[0.0] * 16)
    detector = _detector(vad)
    events = detector.push(_window(1) * 26)

    assert [type(event) for event in events] == [SpeechStart, Utterance]
    utterance = events[1]
    assert isinstance(utterance, Utterance)
    # 10 speech windows plus one 960-byte pad; the 16 silence windows that
    # ended the turn are NOT transcribed.
    assert len(utterance.pcm) == 10 * WINDOW_BYTES + PAD_BYTES
    assert utterance.detected_s == pytest.approx(10 * 32 / 1000)
    assert utterance.peak_probability == 0.9


def test_a_blip_shorter_than_min_speech_is_ignored() -> None:
    # 4 windows = 128 ms, below the 150 ms floor.
    vad = ScriptedVad(*[0.9] * 4, *[0.0] * 16)
    detector = _detector(vad)
    events = detector.push(_window(1) * 20)

    assert [type(event) for event in events] == [SpeechStart, Ignored]
    ignored = events[1]
    assert isinstance(ignored, Ignored)
    assert ignored.duration_s == pytest.approx(4 * 32 / 1000)


def test_probability_between_the_thresholds_keeps_the_segment_alive() -> None:
    # 0.4 is below the 0.5 start threshold but at or above the 0.35 stop
    # threshold, so it must not end the turn.
    vad = ScriptedVad(0.9, *[0.4] * 20, *[0.0] * 16)
    detector = _detector(vad)
    events = detector.push(_window(1) * 37)

    assert [type(event) for event in events] == [SpeechStart, Utterance]
    utterance = events[1]
    assert isinstance(utterance, Utterance)
    assert utterance.detected_s == pytest.approx(21 * 32 / 1000)


def test_pre_roll_survives_into_the_utterance() -> None:
    # The idle windows carry distinct markers so the assertion can tell exactly
    # which bytes were retained.
    vad = ScriptedVad(0.0, 0.0, *[0.9] * 5, *[0.0] * 16)
    detector = _detector(vad)

    events = detector.push(_window(1) + _window(2) + _window(3) * 21)
    utterance = events[-1]
    assert isinstance(utterance, Utterance)
    # 960 bytes of the most recent idle window, then the onset window onwards.
    assert utterance.pcm.startswith(_window(2)[-PAD_BYTES:] + _window(3))
    assert len(utterance.pcm) == PAD_BYTES + 5 * WINDOW_BYTES + PAD_BYTES


def test_max_utterance_force_ends_a_never_ending_turn() -> None:
    vad = ScriptedVad(*[0.9] * 5)
    detector = _detector(vad, max_utterance_s=0.16)
    events = detector.push(_window(1) * 5)

    assert [type(event) for event in events] == [SpeechStart, Utterance]
    utterance = events[-1]
    assert isinstance(utterance, Utterance)
    assert len(utterance.pcm) == 5 * WINDOW_BYTES
    assert len(vad.windows) == 5  # no further window was needed to decide


# --- reset ------------------------------------------------------------------


def test_reset_drops_buffered_audio_and_scoring_state() -> None:
    vad = ScriptedVad(0.0, 0.9, *[0.0] * 16)
    detector = _detector(vad)

    detector.push(bytes(512))  # hold half a window
    detector.reset()
    assert vad.resets == 1

    # The held bytes are gone, so the next full window is window number one.
    detector.push(bytes(WINDOW_BYTES))
    assert len(vad.windows) == 1


# --- SileroVad against the vendored graph -----------------------------------


@pytest.fixture
def silero() -> SileroVad:
    """Load a private graph instance, because this model is stateful.

    Deliberately function-scoped: a shared instance would carry the recurrent
    state from one test into the next, making the tests order-dependent.
    """
    return SileroVad()


def test_silero_vad_rejects_a_window_of_the_wrong_size(silero: SileroVad) -> None:
    with pytest.raises(ValueError, match=str(WINDOW_BYTES)):
        silero.speech_probability(bytes(WINDOW_BYTES - 1))


def test_silero_vad_scores_silence_below_the_threshold(silero: SileroVad) -> None:
    probabilities = [silero.speech_probability(bytes(WINDOW_BYTES)) for _ in range(4)]
    assert all(0.0 <= probability <= 1.0 for probability in probabilities)
    assert max(probabilities) < 0.5


def test_silero_vad_reset_matches_a_fresh_instance(silero: SileroVad) -> None:
    window = bytes(WINDOW_BYTES)

    # Score from a known-zero state so the baseline is genuinely fresh; the
    # fixture scope must not be load-bearing for this test.
    silero.reset()
    fresh = silero.speech_probability(window)

    for _ in range(8):
        silero.speech_probability(b"\x10\x27" * WINDOW_SAMPLES)
    silero.reset()

    # Zeroed state must reproduce the first call exactly, not approximately.
    assert silero.speech_probability(window) == pytest.approx(fresh, abs=1e-6)


def test_endpoint_detector_stays_silent_on_real_silence() -> None:
    detector = _detector(SileroVad())
    assert detector.push(bytes(WINDOW_BYTES * 40)) == []
