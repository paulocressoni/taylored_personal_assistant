"""Speech-to-text for a single voice turn.

The provider is reached through the OpenAI-compatible transcription endpoint, so
Groq (the default) and OpenAI are one code path and differ only in base URL,
model and key. Failures come back as a result object rather than an exception: a
turn that cannot be transcribed should tell the user so, not take the session
down.

The response format is deliberately the plain `json` one. The richer
`verbose_json` would expose Whisper's `no_speech_prob`, which is a more
principled way to spot a hallucination than matching strings, but it is rejected
by the `gpt-4o-transcribe` family and inflates every response with segment
timestamps.
"""

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from openai import AsyncOpenAI

from app.voice.audio import duration_s, wrap_wav

logger = logging.getLogger(__name__)

_REQUEST_FILENAME = "utterance.wav"
_REQUEST_CONTENT_TYPE = "audio/wav"

# Characters Whisper wraps around a transcript that carry no meaning for us.
_TRIM_CHARACTERS = " \t\n.,!?;:'\"()[]-"

# Whisper invents these when handed silence or a non-speech noise. Kept tiny and
# limited to phrases nobody speaks as a command, so a real one-word answer such
# as "yes" — which a smart home assistant needs — is never discarded. Note that
# a GENUINE "thank you" is indistinguishable from an invented one and is dropped
# too; that is the intended trade, see the module docstring.
_SILENCE_HALLUCINATIONS = frozenset(
    {
        "thank you",
        "thank you very much",
        "thanks for watching",
        "thank you for watching",
        "please subscribe",
        "subscribe to my channel",
        "you",
    }
)

# Substrings that only ever come from a non-speech annotation.
_NON_SPEECH_MARKERS = (
    "blank_audio",
    "applause",
    "laughter",
    "subtitles",
    "amara.org",
)


@dataclass(frozen=True)
class Transcript:
    """Result of one transcription attempt.

    Invariant: `text` is empty whenever `error` is set. Callers should check
    `ok` before `is_empty`, so a failed request is never mistaken for silence.

    Attributes:
        text: The transcript, stripped of surrounding whitespace.
        language_hint: The language sent with the request, or `None` when the
            turn had no known language yet.
        duration_s: Length of the audio that was submitted.
        provider: Provider name, for logs and traces.
        error: Description of the failure, or `None` on success.
    """

    text: str
    language_hint: str | None
    duration_s: float
    provider: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when the provider answered, whatever the transcript holds."""
        return self.error is None

    @property
    def is_empty(self) -> bool:
        """True when there is nothing worth sending to the assistant."""
        return not self.text


class Transcriber(Protocol):
    """Minimal speech-to-text surface the voice session depends on."""

    async def transcribe(
        self, pcm: bytes, language_hint: str | None = None
    ) -> Transcript:
        """Transcribe one utterance into text."""
        ...


def _normalise(text: str) -> str:
    """Lower-case a transcript and strip the punctuation around it."""
    return " ".join(text.lower().split()).strip(_TRIM_CHARACTERS)


def _provider_language(language_hint: str | None) -> str | None:
    """Reduce a language tag to the ISO-639-1 code the endpoint expects.

    Our tags carry a region (`pt-BR`) while the endpoint wants only the primary
    subtag (`pt`), and a tag it does not recognise is rejected outright.

    Args:
        language_hint: A tag such as `en`, `de` or `pt-BR`, or `None`.

    Returns:
        The lower-cased primary subtag, or `None` when there is no hint.
    """
    if not language_hint:
        return None
    return language_hint.split("-")[0].lower() or None


def _is_hallucination(text: str) -> bool:
    """Return True when a transcript looks like a Whisper artefact.

    Args:
        text: The raw transcript returned by the provider.

    Returns:
        True when the text carries no words, is a known silence phrase, or names
        a non-speech annotation.
    """
    normalised = _normalise(text)
    if not normalised or not any(character.isalnum() for character in normalised):
        return True
    if normalised in _SILENCE_HALLUCINATIONS:
        return True
    return any(marker in normalised for marker in _NON_SPEECH_MARKERS)


class OpenAICompatibleTranscriber:
    """Transcribe utterances through an OpenAI-compatible audio endpoint.

    Groq is reachable through exactly the same client as OpenAI, so the provider
    is no more than a base URL, a model name and a key.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        base_url: str,
        api_key: str,
        timeout_seconds: float,
        sample_rate: int,
    ) -> None:
        """Create the client and remember the request parameters.

        Args:
            provider: Provider name, echoed into results and logs.
            model: Transcription model name.
            base_url: OpenAI-compatible API root, including the version segment.
            api_key: Provider credential.
            timeout_seconds: Per-request timeout.
            sample_rate: Sample rate of the PCM this instance will be handed.
        """
        self._provider = provider
        self._model = model
        self._sample_rate = sample_rate
        # One retry is deliberate: a turn that is already late recovers by
        # telling the user, not by spending seconds on exponential backoff.
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=1,
        )

    async def transcribe(
        self, pcm: bytes, language_hint: str | None = None
    ) -> Transcript:
        """Transcribe one utterance.

        Args:
            pcm: Mono s16le audio at the configured sample rate.
            language_hint: A tag such as `pt-BR` to bias the model, or `None`.

        Returns:
            The transcript, or a result with `error` set when the provider
            failed or returned only a hallucination.
        """
        audio_s = duration_s(pcm, self._sample_rate)
        # An empty utterance is a valid turn, and the provider rejects it with a 400.
        if not pcm:
            return Transcript(
                text="",
                language_hint=language_hint,
                duration_s=audio_s,
                provider=self._provider,
            )

        # The OpenAI-compatible client does not support a streaming transcription
        # endpoint, so we have to buffer the whole utterance and send it in one request.
        request: dict[str, Any] = {
            "model": self._model,
            "file": (
                _REQUEST_FILENAME,
                wrap_wav(pcm, self._sample_rate),
                _REQUEST_CONTENT_TYPE,
            ),
            "response_format": "json",
        }
        language = _provider_language(language_hint)
        if language:
            # Omitted rather than sent empty: a blank language is a 400.
            request["language"] = language

        try:
            response = await self._client.audio.transcriptions.create(**request)
            text = (response.text or "").strip()
        except Exception as exc:  # noqa: BLE001
            # Tool boundary: a failed transcription is reported to the user, so
            # one bad request must never take the voice session down.
            logger.warning("Transcription via %s failed: %s", self._provider, exc)
            return Transcript(
                text="",
                language_hint=language_hint,
                duration_s=audio_s,
                provider=self._provider,
                error=f"{type(exc).__name__}: {exc}",
            )

        if _is_hallucination(text):
            logger.info(
                "Discarded a likely Whisper artefact from %s: %r",
                self._provider,
                text,
            )
            text = ""

        return Transcript(
            text=text,
            language_hint=language_hint,
            duration_s=audio_s,
            provider=self._provider,
        )
