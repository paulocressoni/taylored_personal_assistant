"""Server-side voice activity detection and endpointing.

Two pieces live here. `SileroVad` wraps the vendored Silero graph into a
per-stream scorer, and `EndpointDetector` turns that stream of probabilities
into turn boundaries. The detector is deliberately the only place that knows
what a "turn" is: clients stay dumb, so the endpointing policy is identical for
a browser tab and for the reSpeaker device.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort

from app.voice.audio import SAMPLE_WIDTH_BYTES, to_float32

SAMPLE_RATE = 16000
WINDOW_SAMPLES = 512
WINDOW_BYTES = WINDOW_SAMPLES * SAMPLE_WIDTH_BYTES
WINDOW_MS = WINDOW_SAMPLES * 1000 / SAMPLE_RATE
CONTEXT_SAMPLES = 64
STATE_SHAPE = (2, 1, 128)
DEFAULT_MODEL_PATH = Path(__file__).resolve().parent / "models" / "silero_vad.onnx"


@dataclass(frozen=True)
class SpeechStart:
    """Speech crossed the threshold; a turn may be starting.

    Emitted at onset rather than after confirmation so playback can be
    interrupted promptly. A following `Ignored` means it never became a turn.
    """


@dataclass(frozen=True)
class Utterance:
    """A complete speech segment, ready for transcription.

    Attributes:
        pcm: The audio to transcribe: pre-roll, speech, and a short tail pad.
        detected_s: How long speech was detected, excluding the silence that
            ended the segment. Less than `pcm` implies, because the trailing
            silence is trimmed off before transcription.
        peak_probability: Highest speech probability seen in the segment.
    """

    pcm: bytes
    detected_s: float
    peak_probability: float


@dataclass(frozen=True)
class Ignored:
    """Speech was detected but discarded for being shorter than `min_speech_ms`.

    Attributes:
        duration_s: How long speech was detected before the blip was dropped.
    """

    duration_s: float


Event = SpeechStart | Utterance | Ignored


class Vad(Protocol):
    """Minimal voice-activity surface the endpoint detector depends on."""

    def reset(self) -> None:
        """Drop all state so the next stream starts fresh."""
        ...

    def speech_probability(self, window: bytes) -> float:
        """Return the speech probability for one fixed-size window."""
        ...


def _ms_to_windows(milliseconds: float) -> int:
    """Convert a duration to whole 32 ms windows, never fewer than one."""
    return max(1, math.ceil(milliseconds / WINDOW_MS))


def _ms_to_bytes(milliseconds: float) -> int:
    """Convert a duration to a byte count of mono s16le samples."""
    return int(milliseconds * SAMPLE_RATE / 1000) * SAMPLE_WIDTH_BYTES


class SileroVad:
    """Silero VAD graph wrapped for a single 16 kHz audio stream.

    The graph is stateful, so an instance belongs to exactly one stream:
    `reset()` is mandatory when a stream restarts, otherwise the new stream
    inherits the previous one's memory. The graph also declares symbolic tensor
    dimensions, so it will not reject a wrongly sized window — the width is
    enforced here instead.
    """

    def __init__(self, model_path: Path | None = None) -> None:
        """Load the graph and zero the per-stream state.

        Args:
            model_path: Path to the vendored ONNX graph; defaults to the copy
                shipped inside this package.
        """
        self._session = ort.InferenceSession(
            str(model_path or DEFAULT_MODEL_PATH),
            providers=["CPUExecutionProvider"],
        )
        self._sample_rate_tensor = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def reset(self) -> None:
        """Zero the recurrent state and the context for a fresh stream."""
        self._state = np.zeros(STATE_SHAPE, dtype=np.float32)
        self._context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)

    def speech_probability(self, window: bytes) -> float:
        """Score one 512-sample window and advance the stream's state.

        Args:
            window: Exactly `WINDOW_BYTES` bytes of 16 kHz mono s16le audio.

        Returns:
            The speech probability in `[0.0, 1.0]`.

        Raises:
            ValueError: if window is not exactly `WINDOW_BYTES` long.
        """
        if len(window) != WINDOW_BYTES:
            raise ValueError(f"window must be exactly {WINDOW_BYTES} bytes")

        samples = to_float32(window).reshape(1, WINDOW_SAMPLES)
        model_input = np.concatenate([self._context, samples], axis=1)
        probabilities, next_state = self._session.run(
            ["output", "stateN"],
            {
                "input": model_input,
                "state": self._state,
                "sr": self._sample_rate_tensor,
            },
        )
        # Explicit reshape: the graph reports this output with unknown rank, so
        # a batch mismatch would otherwise corrupt the next call silently.
        self._state = next_state.reshape(STATE_SHAPE)
        self._context = model_input[:, -CONTEXT_SAMPLES:]
        return float(probabilities[0, 0])


class EndpointDetector:
    """Turn a stream of speech probabilities into turn boundaries.

    Silence is counted in whole 32 ms windows, so `min_silence_ms` rounds up to
    the next multiple of 32; speech is measured in detected audio, so
    `min_speech_ms` is exact.
    """

    def __init__(
        self,
        vad: Vad,
        *,
        threshold: float,
        neg_threshold: float,
        min_speech_ms: float,
        min_silence_ms: float,
        speech_pad_ms: float,
        max_utterance_s: float,
    ) -> None:
        """Configure the state machine.

        Args:
            vad: Scorer for individual windows.
            threshold: Probability above which speech starts.
            neg_threshold: Probability below which speech is considered over.
            min_speech_ms: Segments shorter than this are discarded.
            min_silence_ms: Silence needed after speech before the turn ends.
            speech_pad_ms: Audio kept around the segment so phonemes survive.
            max_utterance_s: Hard cap that force-ends a segment.
        """
        self._vad = vad
        self._threshold = threshold
        self._neg_threshold = neg_threshold
        self._min_speech_ms = min_speech_ms
        self._min_silence_windows = _ms_to_windows(min_silence_ms)
        self._max_utterance_ms = max_utterance_s * 1000
        self._pad_bytes = _ms_to_bytes(speech_pad_ms)

        self._pending = b""
        self._pre_roll = b""
        self._segment = bytearray()
        self._in_speech = False
        self._windows = 0
        self._silence_windows = 0
        self._active_end = 0
        self._peak = 0.0

    def push(self, pcm: bytes) -> list[Event]:
        """Feed captured audio and collect the events it produced.

        Audio may arrive in any chunk size; whole 512-sample windows are carved
        out as they become available and the remainder is held until the next
        call, so no sample is dropped or double-counted.

        Args:
            pcm: Mono 16 kHz s16le audio of any length.

        Returns:
            The events triggered by the newly completed windows, in order.
        """
        buffer = self._pending + pcm
        events: list[Event] = []
        offset = 0
        while len(buffer) - offset >= WINDOW_BYTES:
            window = buffer[offset : offset + WINDOW_BYTES]
            offset += WINDOW_BYTES
            events.extend(self._process(window))
        self._pending = buffer[offset:]
        return events

    def reset(self) -> None:
        """Discard all buffered audio, scoring memory and segment state."""
        self._vad.reset()
        self._pending = b""
        self._pre_roll = b""
        self._clear_segment()

    def _process(self, window: bytes) -> list[Event]:
        """Score one window and advance the state machine."""
        probability = self._vad.speech_probability(window)

        if not self._in_speech:
            if probability <= self._threshold:
                self._remember_pre_roll(window)
                return []
            events: list[Event] = [SpeechStart()]
            self._clear_segment()
            self._in_speech = True
        else:
            events = []

        self._accumulate(window, probability)

        if (
            self._silence_windows >= self._min_silence_windows
            or self._windows * WINDOW_MS >= self._max_utterance_ms
        ):
            events.append(self._finish())
        return events

    def _accumulate(self, window: bytes, probability: float) -> None:
        """Add one window to the open segment and update the tail counters."""
        self._segment += window
        self._windows += 1
        self._peak = max(self._peak, probability)
        if probability >= self._neg_threshold:
            # Hysteresis: anything at or above neg_threshold still counts as
            # speech, so a probability hovering near the threshold cannot cut
            # one sentence into several turns.
            self._active_end = len(self._segment)
            self._silence_windows = 0
        else:
            self._silence_windows += 1

    def _remember_pre_roll(self, window: bytes) -> None:
        """Keep the tail of the idle audio so an onset is not clipped."""
        if self._pad_bytes <= 0:
            return
        self._pre_roll = (self._pre_roll + window)[-self._pad_bytes :]

    def _active_duration_s(self) -> float:
        """Return how long speech was detected, excluding trailing silence."""
        return self._active_end / SAMPLE_WIDTH_BYTES / SAMPLE_RATE

    def _finish(self) -> Utterance | Ignored:
        """Close the open segment, classify it and emit the matching event."""
        detected_s = self._active_duration_s()

        if detected_s * 1000 < self._min_speech_ms:
            event: Utterance | Ignored = Ignored(duration_s=detected_s)
        else:
            # Cut the ending silence run back to the pad. Everything past the
            # last active window is what ENDED the turn, and STT pays for every
            # byte of it.
            end = min(len(self._segment), self._active_end + self._pad_bytes)
            event = Utterance(
                pcm=self._pre_roll + bytes(self._segment[:end]),
                detected_s=detected_s,
                peak_probability=self._peak,
            )

        self._pre_roll = b""
        self._clear_segment()
        return event

    def _clear_segment(self) -> None:
        """Drop the open segment without emitting anything."""
        self._in_speech = False
        self._segment = bytearray()
        self._windows = 0
        self._silence_windows = 0
        self._active_end = 0
        self._peak = 0.0
