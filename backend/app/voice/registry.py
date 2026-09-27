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

from app.core.config import settings
from app.voice.stt import OpenAICompatibleTranscriber, Transcriber
from app.voice.tts import OpenAISynthesizer, Synthesizer
from app.voice.vad import SileroVad, Vad

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VoiceProviders:
    """The long-lived voice collaborators, built once per process.

    Attributes:
        transcriber: Shared speech-to-text client.
        synthesizer: Shared text-to-speech client.
        vad_factory: Builds a NEW VAD for each socket, because a VAD owns
            per-stream state.
    """

    transcriber: Transcriber
    synthesizer: Synthesizer
    vad_factory: Callable[[], Vad]


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
    )
