"""Server-side wake-word detection in front of the voice pipeline.

Why this exists: the satellite streams continuously once connected, so without a
gate every sound in the room becomes a paid transcription and a graph run. The
gate belongs on the server because the server already owns endpointing, because
one gate then serves the browser, the simulator and the device, and because the
phrase must be tunable without reflashing firmware.

How the detection works — three small ONNX graphs, in order:

  1. `melspectrogram.onnx` turns a 0.79 s slice of 16 kHz audio into 76 frames of
     32 mel bins. Exactly 12 640 samples produce exactly 76 frames.
  2. `embedding_model.onnx` turns those 76x32 frames into 96 floats (a Google
     `speech_embedding`-style feature vector).
  3. `hey_jarvis_v0.1.onnx` scores the last 16 embeddings (1.28 s of audio) from
     0 to 1. One new embedding is produced per 80 ms hop.

The graphs are vendored and called through `onnxruntime` directly, mirroring
`app/voice/vad.py`: the PyPI package cannot be installed here (it requires
`tflite-runtime`, which has no wheel for this project's Python), and this way the
models never change underneath a running service.

Two traps this module exists to keep in one place:

  - Samples must be `float32` carrying **int16 amplitude**, not the `[-1, 1)`
    form `app.voice.audio.to_float32` produces. The mel graph does not complain;
    it just returns values ~90 dB too quiet and every score collapses to zero.
  - The mel output must be transformed with `x/10 + 2` before the embedding
    graph, as upstream does. Skipping it produces plausible-looking embeddings
    and meaningless scores.

`WakeGate` is the policy half: a detection arms a permit that allows exactly one
speech segment to become a turn. It lives here rather than in the session so the
window arithmetic can be tested with an injected clock and no audio at all.
"""

import logging
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
import onnxruntime as ort

from app.core.timing import monotonic_now
from app.voice.audio import int16_view

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH_BYTES = 2

# The classifier's step: one new embedding, and therefore one new score, per
# 1280 samples. 80 ms is also the latency floor of a detection.
HOP_SAMPLES = 1280
HOP_BYTES = HOP_SAMPLES * SAMPLE_WIDTH_BYTES

# The embedding model's receptive field: 76 mel frames at a 10 ms hop plus one
# 30 ms analysis window, i.e. 12 640 samples. Verified against the graph: feeding
# exactly this many samples returns exactly 76 frames.
WINDOW_FRAMES = 76
MEL_BINS = 32
ANALYSIS_SAMPLES = 12_640
ANALYSIS_BYTES = ANALYSIS_SAMPLES * SAMPLE_WIDTH_BYTES

# The classifier always sees 16 embeddings (1.28 s) of 96 floats.
FEATURE_ROWS = 16
FEATURE_WIDTH = 96

# How long a rejected segment is kept on the chance that the detection for the
# phrase inside it arrives just after the endpointer closed it. The endpointer
# pads 500 ms of silence by default and the detector steps every 80 ms, so this
# only ever covers ordering skew — it is not a tunable behaviour.
WAKE_GRACE_SECONDS = 0.5

DEFAULT_MODEL_DIR = Path(__file__).resolve().parent / "models" / "openwakeword"
DEFAULT_MODEL = "hey_jarvis_v0.1.onnx"

# Spoken text of each bundled model, used only to strip the phrase from the
# transcript before the graph sees it. A model without an entry simply never
# strips — harmless, because the LLM answers the command anyway — but the config
# validator refuses that combination at startup so it cannot pass unnoticed.
PHRASES: Mapping[str, str] = {
    "hey_jarvis_v0.1.onnx": "hey jarvis",
}

# What a user says to abandon the turn in flight. Matched against the STRIPPED,
# normalised transcript, gated sessions only: an ungated push-to-talk "stop" is
# a normal question and still gets an answer.
CANCEL_PHRASES = frozenset(
    {
        "stop",
        "stop it",
        "stop now",
        "quiet",
        "be quiet",
        "cancel",
        "never mind",
        "nevermind",
        "stopp",
        "shut up",
        "hör auf",
        "sei still",
        "halt",
        "klappe",
        "pare",
        "para",
        "silêncio",
        "cancela",
        "cala boca",
    }
)

# Punctuation becomes a space so "Hey, Jarvis!" and "hey jarvis" normalise alike.
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_LEADING_PUNCTUATION = " \t,.;:!?—–-"


@dataclass(frozen=True)
class Detection:
    """One accepted wake-word detection.

    Attributes:
        score: The classifier's probability for the hop that crossed the
            threshold. Logged, and the number used when tuning the threshold.
    """

    score: float


class WakeWordUnavailable(RuntimeError):
    """The wake detector failed and the session cannot continue safely.

    Raised instead of silently running un-gated: a session that believes it is
    gated must never stream ambient audio to a paid STT. The voice route catches
    it and closes the socket with 1011, which is the only signal the satellite
    acts on — it reconnects with backoff and builds a fresh detector.
    """


class WakeWord(Protocol):
    """Per-stream wake-word detector, the seam the voice session depends on."""

    def reset(self) -> None:
        """Drop all buffered audio and history so a new stream starts clean."""
        ...

    def push(self, pcm: bytes) -> list[Detection]:
        """Feed audio and collect the detections it completed.

        Args:
            pcm: Mono 16 kHz s16le audio of any length.

        Returns:
            The detections completed by this chunk, in order. A chunk smaller
            than one hop returns an empty list; the remainder is kept.
        """
        ...


class WakeScorer(Protocol):
    """Feature extraction and scoring, i.e. everything that needs the graphs.

    Splitting this out of the detector is what lets the detector be tested with
    scripted numbers instead of ONNX — the buffering, warm-up and debounce rules
    are the parts worth testing, and they are independent of the model.
    """

    def embed(self, window: bytes) -> np.ndarray:
        """Return the 96-float embedding for one 12 640-sample window."""
        ...

    def classify(self, rows: np.ndarray) -> float:
        """Return the wake probability for the last 16 embeddings."""
        ...


def _normalise(text: str) -> str:
    """Lowercase text and reduce punctuation to single spaces.

    One normaliser for both phrase stripping and cancel matching, so a configured
    phrase like "Hey, Jarvis!" strips exactly like the default and "Stop!" hits
    the cancel set.

    Args:
        text: Any transcript or configured phrase.

    Returns:
        The comparable form: words separated by single spaces.
    """
    return " ".join(_NON_WORD.sub(" ", text).casefold().split())


def strip_wake_phrase(text: str, phrase: str) -> str:
    """Remove a leading wake phrase from a transcript.

    Works on the original text so the caller keeps the user's casing and
    punctuation for everything after the phrase. Only a leading match is
    removed: a phrase buried mid-sentence is left alone, and the graph answers
    the command anyway.

    Args:
        text: The transcript as the STT provider returned it.
        phrase: The spoken form of the configured model, e.g. "hey jarvis".

    Returns:
        The transcript without the leading phrase, an empty string when the
        transcript was only the phrase, or `text` unchanged when it did not
        start with the phrase.
    """
    phrase_words = phrase.split()
    text_words = text.split()
    if not phrase_words or len(text_words) < len(phrase_words):
        return text
    head = " ".join(text_words[: len(phrase_words)])
    if _normalise(head) != _normalise(phrase):
        return text
    return (
        " ".join(text_words[len(phrase_words) :]).lstrip(_LEADING_PUNCTUATION).strip()
    )


def is_cancel_phrase(text: str) -> bool:
    """Return True when a stripped transcript is a request to stop.

    Args:
        text: The transcript after `strip_wake_phrase`.

    Returns:
        True for an exact cancel phrase, ignoring case and punctuation.
    """
    return _normalise(text) in CANCEL_PHRASES


class OpenWakeWordModels:
    """The three ONNX graphs, loaded once per process and shared by all sockets.

    The graphs are stateless; all per-stream state lives in
    `OpenWakeWordDetector`. Threads are pinned to one because the graphs are tiny
    and the default pool oversubscribes as soon as a second socket opens: the
    measured cost is ~1.9 ms per 80 ms hop single-threaded, and the same
    measurement with a pool is slower and noisier, not faster.

    Args:
        model_name: Filename of the wake-word graph inside `model_dir`.
        model_dir: Directory holding the three graphs; defaults to the copy
            shipped with the package.

    Raises:
        RuntimeError: if a graph is missing or cannot be loaded.
    """

    def __init__(
        self, model_name: str = DEFAULT_MODEL, model_dir: Path | None = None
    ) -> None:
        """Load and validate the graphs, then pre-compute the silence seed."""
        directory = model_dir or DEFAULT_MODEL_DIR
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1

        self._mel = _load_graph(directory / "melspectrogram.onnx", options)
        self._embedding = _load_graph(directory / "embedding_model.onnx", options)
        self._classifier = _load_graph(directory / model_name, options)

        # Names are read from the graphs rather than hard-coded: they are an
        # implementation detail of the exporter, and the ONNX runtime reports
        # them faithfully.
        self._mel_input = self._mel.get_inputs()[0].name
        self._embedding_input = self._embedding.get_inputs()[0].name
        self._classifier_input = self._classifier.get_inputs()[0].name

        # The embedding of a silent 0.79 s window, used to seed the detector's
        # history so a session can classify as soon as it has real audio. It is
        # computed here, once, rather than hard-coded, so a model update cannot
        # leave a stale seed behind.
        self._silence = self.embed(bytes(ANALYSIS_BYTES))

    @property
    def silence_embedding(self) -> np.ndarray:
        """A copy of the 96-float embedding of digital silence."""
        return self._silence.copy()

    def embed(self, window: bytes) -> np.ndarray:
        """Embed one analysis window.

        Args:
            window: Exactly `ANALYSIS_BYTES` bytes of 16 kHz mono s16le audio.

        Returns:
            A 96-float feature vector.

        Raises:
            ValueError: if the window is the wrong size, or if the mel graph
                returns a shape the embedding graph cannot consume.
        """
        if len(window) != ANALYSIS_BYTES:
            raise ValueError(
                f"window must be exactly {ANALYSIS_BYTES} bytes, got {len(window)}"
            )

        # Deliberately NOT `to_float32`: that normalises to [-1, 1), and the mel
        # graph expects int16 amplitude. The difference is a ~90 dB offset that
        # turns every score into zero without raising anything.
        samples = int16_view(window).astype(np.float32)[None, :]
        mel = self._mel.run(None, {self._mel_input: samples})[0]
        frames = np.squeeze(mel)
        if frames.shape != (WINDOW_FRAMES, MEL_BINS):
            # Symbolic dimensions mean ONNX Runtime will not reject a wrong
            # width; failing here beats feeding the embedding graph garbage.
            raise ValueError(f"mel spectrogram shape {frames.shape} is unexpected")

        features = (frames / 10.0) + 2.0
        embedded = self._embedding.run(
            None, {self._embedding_input: features[None, :, :, None]}
        )[0]
        return np.asarray(embedded, dtype=np.float32).reshape(FEATURE_WIDTH)

    def classify(self, rows: np.ndarray) -> float:
        """Score the last 16 embeddings.

        Args:
            rows: A `(16, 96)` array, oldest row first.

        Returns:
            The wake probability in `[0.0, 1.0]`.

        Raises:
            ValueError: if `rows` is not `(16, 96)`.
        """
        batch = np.asarray(rows, dtype=np.float32)
        if batch.shape != (FEATURE_ROWS, FEATURE_WIDTH):
            raise ValueError(f"features must be ({FEATURE_ROWS}, {FEATURE_WIDTH})")
        scored = self._classifier.run(
            None, {self._classifier_input: batch[None, :, :]}
        )[0]
        return float(np.asarray(scored).reshape(-1)[0])


def _load_graph(path: Path, options: ort.SessionOptions) -> ort.InferenceSession:
    """Load one graph, failing with an operator-facing message if it is absent.

    Raises:
        RuntimeError: if the file is missing — deliberately not
            `FileNotFoundError`, so a bad deploy surfaces as one config line
            instead of an ONNX traceback from inside the lifespan.
    """
    if not path.is_file():
        raise RuntimeError(
            f"wake word model {path.name} is missing from {path.parent} — "
            "run backend/scripts/download_wake_word_models.py"
        )
    return ort.InferenceSession(str(path), options, providers=["CPUExecutionProvider"])


class OpenWakeWordDetector:
    """Turn one audio stream into wake detections.

    Audio may arrive in any chunk size; whole 80 ms hops are carved out as they
    become available and the remainder is kept for the next call, so no sample is
    dropped. The first `ANALYSIS_SAMPLES` bytes fill the mel window; nothing is
    scored before that, because a short window would produce a different number
    of mel frames and meaningless embeddings.

    Args:
        scorer: Feature extraction and classification (the graphs, or a fake).
        threshold: Score at or above which a detection is reported.
        debounce_s: How long to ignore further crossings. A suppressed
            re-detection is harmless: the permit it would arm is already open.
        seed: The 16-row history to start from, normally the embedding of
            silence. Without it the detector waits for 16 real embeddings.
        now: Clock used for debounce. Injected so tests need no sleeping.

    Raises:
        ValueError: if `seed` is not 96 floats.
    """

    def __init__(
        self,
        scorer: WakeScorer,
        *,
        threshold: float,
        debounce_s: float,
        seed: np.ndarray | None = None,
        now: Callable[[], float] = monotonic_now,
    ) -> None:
        """Store the policy and prepare empty stream state."""
        self._scorer = scorer
        self._threshold = threshold
        self._debounce_s = debounce_s
        self._now = now
        self._seed: np.ndarray | None = None
        if seed is not None:
            candidate = np.asarray(seed, dtype=np.float32).reshape(-1)
            if candidate.size != FEATURE_WIDTH:
                raise ValueError(f"seed must hold {FEATURE_WIDTH} floats")
            # Read-only: the same array is reused for every seeded row.
            candidate.flags.writeable = False
            self._seed = candidate

        self._pending = bytearray()
        self._window = bytearray()
        self._features: deque[np.ndarray] = deque(maxlen=FEATURE_ROWS)
        self._last_detection: float | None = None
        self.reset()

    def reset(self) -> None:
        """Discard buffered audio, history and the debounce state."""
        self._pending.clear()
        self._window.clear()
        self._features.clear()
        self._last_detection = None
        if self._seed is not None:
            for _ in range(FEATURE_ROWS):
                self._features.append(self._seed)

    def push(self, pcm: bytes) -> list[Detection]:
        """Feed audio and report the detections it completed.

        Args:
            pcm: Mono 16 kHz s16le audio of any length.

        Returns:
            One `Detection` per accepted crossing, in order.
        """
        detections: list[Detection] = []
        for score in self._consume(pcm):
            if score < self._threshold:
                continue
            at = self._now()
            if (
                self._last_detection is not None
                and at - self._last_detection < self._debounce_s
            ):
                continue
            self._last_detection = at
            detections.append(Detection(score=score))
        return detections

    def push_scores(self, pcm: bytes) -> list[float]:
        """Feed audio and return every hop's raw score, threshold-free.

        Tooling only — `wake_word_eval.py` needs the score distribution, and
        lowering the threshold to zero would conflate "a score" with "a
        detection" and still hide scores below it.

        Args:
            pcm: Mono 16 kHz s16le audio of any length.

        Returns:
            One score per completed hop that had a full feature history.
        """
        return self._consume(pcm)

    def _consume(self, pcm: bytes) -> list[float]:
        """Advance the buffers by whole hops and score each completed one."""
        self._pending.extend(pcm)
        scores: list[float] = []
        while len(self._pending) >= HOP_BYTES:
            hop = bytes(self._pending[:HOP_BYTES])
            del self._pending[:HOP_BYTES]

            self._window.extend(hop)
            if len(self._window) > ANALYSIS_BYTES:
                del self._window[:-ANALYSIS_BYTES]
            if len(self._window) < ANALYSIS_BYTES:
                continue

            self._features.append(self._scorer.embed(bytes(self._window)))
            if len(self._features) < FEATURE_ROWS:
                continue
            scores.append(self._scorer.classify(np.vstack(self._features)))
        return scores


class WakeGate:
    """The permit that lets a speech segment become a turn.

    One detection arms one permit. `admit` both decides and consumes, so a caller
    cannot accept a segment without closing the gate — the bug that would let a
    television cut an answer off at second three and accept "and tomorrow?" at
    second five.

    Args:
        window_s: How long a permit stays valid after it is armed.
        now: Clock, injected so window boundaries are testable without sleeping.
    """

    def __init__(
        self, window_s: float, *, now: Callable[[], float] = monotonic_now
    ) -> None:
        """Start closed with no permit armed."""
        self._window_s = window_s
        self._now = now
        self._deadline: float | None = None
        self._last_arm_at: float | None = None

    @property
    def last_arm_at(self) -> float | None:
        """When the current (or most recent) permit was armed, if ever."""
        return self._last_arm_at

    def arm(self) -> None:
        """Open a permit for `window_s`, replacing any previous one."""
        self._last_arm_at = self._now()
        self._deadline = self._last_arm_at + self._window_s

    def is_open(self, at: float | None = None) -> bool:
        """Return whether a permit is valid, without consuming it.

        Args:
            at: The instant to test; defaults to now. Used with an onset time by
                the caller that decides whether to forward `speech_start`.

        Returns:
            True while a permit is armed and unexpired.
        """
        if self._deadline is None:
            return False
        return (self._now() if at is None else at) <= self._deadline

    def admit(self, started_at: float) -> bool:
        """Decide whether a segment may run, consuming the permit when it may.

        Args:
            started_at: When the segment's first speech window began.

        Returns:
            True when the segment started inside the permit — in which case the
            permit is now consumed — and False otherwise.
        """
        if not self.is_open(started_at):
            return False
        self._deadline = None
        return True

    def remaining(self) -> float:
        """Seconds left on the permit, or 0.0 when it is closed."""
        if self._deadline is None:
            return 0.0
        return max(0.0, self._deadline - self._now())
