"""Process-wide assembly of the voice collaborators.

Every socket needs a transcriber and a synthesizer, and both own an HTTP
connection pool, so they are built ONCE here and shared — the same reasoning that
compiles the graph once in `app.main`.

The VAD is the exception. `SileroVad` holds the graph's recurrent state and the
endpointer holds a segment buffer, so one instance per socket is not an
optimisation but a correctness requirement: a shared VAD would fold two
conversations' audio into the same model state. The registry therefore hands out
a FACTORY, never an instance.

`app.main` calls these once and stores the results on `app.state`. Nothing here
is a module global, which is what lets a test replace the providers through
app.state instead of monkeypatching two vendors.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from app.core.config import settings
from app.core.timing import monotonic_now
from app.voice.stt import OpenAICompatibleTranscriber, Transcriber
from app.voice.tts import OpenAISynthesizer, Synthesizer
from app.voice.vad import SileroVad, Vad
from app.voice.wake import (
    ANALYSIS_BYTES,
    PHRASES,
    OpenWakeWordDetector,
    OpenWakeWordModels,
    WakeWord,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceProviders:
    """The long-lived voice collaborators, built once per process.

    Attributes:
        transcriber: Shared speech-to-text client.
        synthesizer: Shared text-to-speech client.
        vad_factory: Builds a NEW VAD for each socket, because a VAD owns
            per-stream state.
        wake_word_factory: Builds a NEW wake detector for each socket, because the
            detector owns per-stream history. None when the feature is off, which
            is what makes an explicit `wake_word: "on"` refusable.
        wake_phrase: The spoken phrase the detector listens for, used to strip it
            from the transcript. Resolved once, here, from the setting or the
            model's built-in phrase.
    """

    transcriber: Transcriber
    synthesizer: Synthesizer
    vad_factory: Callable[[], Vad]
    wake_word_factory: Callable[[], WakeWord] | None = None
    wake_phrase: str = ""


class SessionSlots:
    """Counts live voice sockets and refuses to exceed the configured cap.

    A plain counter rather than an `asyncio.Semaphore`: a semaphore would make
    the next caller WAIT for a slot, and a socket that cannot start should be told
    so and closed instead of hanging invisibly in a queue. No lock is needed
    either, because `acquire` checks and increments with no await in between, so
    no other task can interleave.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active = 0

    @property
    def active(self) -> int:
        """How many slots are currently held."""
        return self._active

    def acquire(self) -> bool:
        """Take a slot.

        Returns:
            True when one was free and is now held, False when the cap is met.
        """
        if self._active >= self._limit:
            return False
        self._active += 1
        return True

    def release(self) -> None:
        """Give a slot back. Releasing without holding one is a no-op."""
        self._active = max(0, self._active - 1)


def build_vad() -> Vad:
    """Build a VAD for ONE audio stream.

    Each call loads the ONNX graph into its own inference session, which is the
    price of per-stream state at a socket cap of a handful. Hoisting the session
    out of `SileroVad` would remove that cost and is only worth doing if the cap
    grows.

    Returns:
        A fresh, unshared VAD.
    """
    return SileroVad()


def _measure_wake_realtime(models: OpenWakeWordModels) -> float:
    """Return how many seconds of audio the detector scores per second of CPU.

    Measured once at startup rather than per session: it is a property of the
    host, and the number belongs in the log where a deployment can see its
    headroom. Three seconds of silence is enough for a stable ratio and costs
    about 80 ms.

    Args:
        models: The loaded graphs to score with.

    Returns:
        The realtime factor (audio seconds per wall second), or 0.0 when the
        clock produced no measurable interval.
    """
    detector = OpenWakeWordDetector(
        models,
        threshold=1.0,
        debounce_s=0.0,
        seed=models.silence_embedding,
    )
    audio = bytes(ANALYSIS_BYTES + 3 * 16000 * 2)
    started = monotonic_now()
    detector.push_scores(audio)
    elapsed = monotonic_now() - started
    return (3.0 / elapsed) if elapsed > 0 else 0.0


def build_voice_providers() -> VoiceProviders | None:
    """Build the process-wide voice clients.

    Returns:
        The providers, or None when voice is switched off or not fully keyed. The
        config validator already refuses to start when voice is on and a key is
        missing, so None here means "voice is off" and the caller must treat a
        voice socket as unsupported rather than half-configured.
    """
    if not settings.voice_ready:
        return None

    wake_word_factory: Callable[[], WakeWord] | None = None
    wake_phrase = ""
    if settings.voice_wake_word_enabled:
        # Loaded ONCE for the process: the graphs are stateless, and re-loading
        # them per socket would add ~10 ms and a second copy of each graph.
        models = OpenWakeWordModels(settings.voice_wake_word_model)

        # The phrase is resolved here rather than in Settings so that
        # `import app.core.config` stays free of numpy/onnxruntime. An empty
        # phrase with an unknown model would strip nothing and look like a broken
        # feature, so it is a startup failure instead.
        wake_phrase = settings.voice_wake_word_phrase.strip() or PHRASES.get(
            settings.voice_wake_word_model, ""
        )
        if not wake_phrase:
            raise RuntimeError(
                f"VOICE_WAKE_WORD_PHRASE is empty and "
                f"{settings.voice_wake_word_model} has no built-in phrase; set "
                "the phrase explicitly."
            )

        logger.info(
            "voice wake detector ready (realtime=%.1fx, threshold=%.2f, phrase=%r)",
            _measure_wake_realtime(models),
            settings.voice_wake_word_threshold,
            wake_phrase,
        )
        wake_word_factory = partial(
            OpenWakeWordDetector,
            models,
            threshold=settings.voice_wake_word_threshold,
            debounce_s=settings.voice_wake_word_debounce_seconds,
            seed=models.silence_embedding,
        )

    return VoiceProviders(
        transcriber=OpenAICompatibleTranscriber(
            provider=settings.voice_stt_provider,
            model=settings.voice_stt_model,
            base_url=settings.voice_stt_base_url,
            api_key=settings.voice_stt_api_key.get_secret_value(),
            timeout_seconds=settings.voice_stt_timeout_seconds,
            sample_rate=settings.voice_input_sample_rate,
        ),
        synthesizer=OpenAISynthesizer(
            model=settings.voice_tts_model,
            base_url=settings.voice_tts_base_url,
            api_key=settings.voice_tts_api_key.get_secret_value(),
            instructions=settings.voice_tts_instructions,
            timeout_seconds=settings.voice_tts_timeout_seconds,
        ),
        vad_factory=build_vad,
        wake_word_factory=wake_word_factory,
        wake_phrase=wake_phrase,
    )
