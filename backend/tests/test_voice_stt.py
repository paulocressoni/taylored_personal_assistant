"""Unit tests for the OpenAI-compatible transcriber — no network, no key."""

import io
import wave
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from app.voice.stt import OpenAICompatibleTranscriber, Transcript

# Arbitrary inputs for the plumbing assertions below — deliberately NOT the
# shipped defaults, which are pinned once in test_config.py. Keeping them
# synthetic is also what makes the assertions meaningful: a value that differs
# from the default proves the constructor honours what it is given rather than
# something hardcoded inside the transcriber.
PROVIDER = "test-provider"
MODEL_NAME = "test-whisper"
BASE_URL = "http://stt.invalid/v1"
SAMPLE_RATE = 8000
ONE_SECOND = bytes(2 * SAMPLE_RATE)


class FakeTranscriptions:
    """Records each request and replays a scripted response or exception."""

    def __init__(self, result: Any) -> None:
        self._result = result
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeClient:
    """Stand-in for `AsyncOpenAI` that also captures constructor arguments."""

    def __init__(self, result: Any, **init_kwargs: Any) -> None:
        self.init_kwargs = init_kwargs
        self.transcriptions = FakeTranscriptions(result)
        self.audio = SimpleNamespace(transcriptions=self.transcriptions)


BuildTranscriber = Callable[[Any], tuple[OpenAICompatibleTranscriber, FakeClient]]


@pytest.fixture
def build_transcriber(monkeypatch: pytest.MonkeyPatch) -> BuildTranscriber:
    """Build a transcriber whose provider call is faked, mirroring `patch_llm`."""

    def _build(result: Any) -> tuple[OpenAICompatibleTranscriber, FakeClient]:
        built: list[FakeClient] = []

        def _make_client(**kwargs: Any) -> FakeClient:
            instance = FakeClient(result, **kwargs)
            built.append(instance)
            return instance

        monkeypatch.setattr("app.voice.stt.AsyncOpenAI", _make_client)
        transcriber = OpenAICompatibleTranscriber(
            provider=PROVIDER,
            model=MODEL_NAME,
            base_url=BASE_URL,
            api_key="gsk-test",  # pragma: allowlist secret
            timeout_seconds=5.0,
            sample_rate=SAMPLE_RATE,
        )
        return transcriber, built[0]

    return _build


# --- happy path -------------------------------------------------------------


async def test_transcribe_returns_the_provider_text(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, _ = build_transcriber(SimpleNamespace(text="  Turn on the lamp.  "))
    transcript = await transcriber.transcribe(ONE_SECOND)

    assert isinstance(transcript, Transcript)
    assert transcript.text == "Turn on the lamp."
    assert transcript.ok is True
    assert transcript.error is None


async def test_transcribe_reports_the_audio_duration_and_provider(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, _ = build_transcriber(SimpleNamespace(text="Hello."))
    transcript = await transcriber.transcribe(ONE_SECOND * 2)

    assert transcript.duration_s == pytest.approx(2.0)
    assert transcript.provider == PROVIDER


async def test_transcribe_sends_a_wav_container_and_the_configured_model(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, client = build_transcriber(SimpleNamespace(text="Hello."))
    await transcriber.transcribe(ONE_SECOND)

    request = client.transcriptions.requests[0]
    assert request["model"] == MODEL_NAME
    assert request["response_format"] == "json"

    filename, payload, content_type = request["file"]
    assert filename == "utterance.wav"
    assert content_type == "audio/wav"
    # Raw PCM has no header, so the provider could not identify the format.
    with wave.open(io.BytesIO(payload), "rb") as wav:
        assert wav.getframerate() == SAMPLE_RATE
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == SAMPLE_RATE


async def test_transcribe_carries_the_credentials_into_the_client(
    build_transcriber: BuildTranscriber,
) -> None:
    _, client = build_transcriber(SimpleNamespace(text="Hello."))

    # Groq is reached through the OpenAI-compatible client, so base_url is what
    # makes the vendor a configuration choice.
    assert client.init_kwargs["base_url"] == BASE_URL
    assert client.init_kwargs["max_retries"] == 1


# --- language hint ----------------------------------------------------------


async def test_transcribe_hints_the_primary_language_subtag(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, client = build_transcriber(SimpleNamespace(text="Ola."))
    transcript = await transcriber.transcribe(ONE_SECOND, language_hint="pt-BR")

    assert client.transcriptions.requests[0]["language"] == "pt"
    assert transcript.language_hint == "pt-BR"


async def test_transcribe_omits_the_language_when_none_is_known(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, client = build_transcriber(SimpleNamespace(text="Hello."))
    await transcriber.transcribe(ONE_SECOND)

    assert "language" not in client.transcriptions.requests[0]


# --- failure and emptiness --------------------------------------------------


async def test_transcribe_never_raises_on_a_provider_error(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, _ = build_transcriber(RuntimeError("connection reset"))
    transcript = await transcriber.transcribe(ONE_SECOND)

    assert transcript.ok is False
    assert transcript.text == ""
    assert transcript.error is not None
    assert "RuntimeError" in transcript.error
    assert "connection reset" in transcript.error


async def test_transcribe_treats_whitespace_only_text_as_empty(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, _ = build_transcriber(SimpleNamespace(text="   \n "))
    transcript = await transcriber.transcribe(ONE_SECOND)

    assert transcript.ok is True
    assert transcript.text == ""
    assert transcript.is_empty is True


async def test_transcribe_skips_the_provider_when_there_is_no_audio(
    build_transcriber: BuildTranscriber,
) -> None:
    transcriber, client = build_transcriber(SimpleNamespace(text="Hello."))
    transcript = await transcriber.transcribe(b"")

    assert client.transcriptions.requests == []
    assert transcript.ok is True
    assert transcript.is_empty is True
    assert transcript.duration_s == 0.0


# --- hallucination filtering ------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Thank you.",
        "Thanks for watching!",
        " [BLANK_AUDIO] ",
        "subtitles by amara.org",
        "...",
        "\u266a\u266a\u266a",
    ],
)
async def test_transcribe_discards_whisper_artefacts(
    build_transcriber: BuildTranscriber,
    text: str,
) -> None:
    transcriber, _ = build_transcriber(SimpleNamespace(text=text))
    transcript = await transcriber.transcribe(ONE_SECOND)

    assert transcript.text == ""
    assert transcript.is_empty is True


@pytest.mark.parametrize(
    "text",
    ["Yes.", "Turn off the kitchen light.", "Danke!", "Apaga a luz."],
)
async def test_transcribe_keeps_short_but_genuine_utterances(
    build_transcriber: BuildTranscriber,
    text: str,
) -> None:
    # The counter-test to the filter above: a one-word confirmation and a
    # non-English answer must both survive it.
    transcriber, _ = build_transcriber(SimpleNamespace(text=text))
    transcript = await transcriber.transcribe(ONE_SECOND)

    assert transcript.text == text
    assert transcript.is_empty is False
