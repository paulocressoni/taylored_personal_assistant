"""Unit tests for sentence chunking and the streaming synthesizer."""

from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace
from typing import Any, Self

import pytest

from app.voice.tts import PCM_SAMPLE_RATE, OpenAISynthesizer, SentenceSplitter

MODEL_NAME = "test-speech"
BASE_URL = "http://tts.invalid/v1"
VOICE = "test-voice"
INSTRUCTIONS = "Speak plainly."


def _splitter(min_chars: int = 5, max_chars: int = 200) -> SentenceSplitter:
    """Build a splitter with test-sized bounds."""
    return SentenceSplitter(min_chars=min_chars, max_chars=max_chars)


def test_pcm_sample_rate_matches_the_provider_contract() -> None:
    # The session resamples from this rate when a device asks for 16 kHz.
    assert PCM_SAMPLE_RATE == 24000


# --- SentenceSplitter: boundaries -------------------------------------------


def test_a_complete_sentence_is_emitted_immediately() -> None:
    assert _splitter().push("Hello there.") == ["Hello there."]


def test_an_incomplete_sentence_is_held_back() -> None:
    splitter = _splitter()
    assert splitter.push("The kitchen light is") == []
    assert splitter.push(" now on.") == ["The kitchen light is now on."]


def test_a_fragment_shorter_than_min_chars_joins_the_next_sentence() -> None:
    # Regression guard: a naive "wait until long enough" rule stalls forever
    # here, because the short sentence never grows by itself.
    splitter = _splitter(min_chars=20)
    assert splitter.push("Yes. And the kitchen light is on.") == [
        "Yes. And the kitchen light is on."
    ]


def test_question_and_exclamation_marks_end_sentences() -> None:
    chunks = _splitter().push("Are you sure? Of course!")
    assert chunks == ["Are you sure?", "Of course!"]


def test_consecutive_terminators_stay_with_their_sentence() -> None:
    assert _splitter(min_chars=8).push("Really?! I doubt it.")[0] == "Really?!"


def test_a_closing_quote_stays_with_its_sentence() -> None:
    assert _splitter().push('"Hello." Then he left.')[0] == '"Hello."'


def test_a_newline_ends_a_chunk_without_needing_a_space() -> None:
    assert _splitter().push("First line\nSecond") == ["First line"]


def test_an_ellipsis_is_not_split_inside_itself() -> None:
    assert _splitter().push("Wait... really?") == ["Wait...", "really?"]


# --- SentenceSplitter: false terminators ------------------------------------


def test_a_decimal_point_is_not_a_sentence_end() -> None:
    assert _splitter().push("The temperature is 21.5 degrees.") == [
        "The temperature is 21.5 degrees."
    ]


def test_an_abbreviation_is_not_a_sentence_end() -> None:
    assert _splitter().push("Dr. Smith will see you now.") == [
        "Dr. Smith will see you now."
    ]


def test_an_initial_is_not_a_sentence_end() -> None:
    assert _splitter().push("J. Silva called.") == ["J. Silva called."]


def test_a_domain_is_not_split_in_half() -> None:
    assert _splitter().push("Visit example.com for details.") == [
        "Visit example.com for details."
    ]


# --- SentenceSplitter: bounds and flush -------------------------------------


def test_text_without_punctuation_is_split_at_a_word_boundary() -> None:
    splitter = _splitter(min_chars=5, max_chars=20)
    chunks = splitter.push("word " * 10)

    assert chunks
    assert all(len(chunk) <= 20 for chunk in chunks)
    # No chunk may contain a torn word.
    assert all(set(chunk.split()) == {"word"} for chunk in chunks)


def test_flush_emits_the_tail_that_has_no_terminator() -> None:
    splitter = _splitter()
    assert splitter.push("No punctuation here") == []
    assert splitter.flush() == ["No punctuation here"]


def test_flush_drains_the_buffer_so_it_is_idempotent() -> None:
    splitter = _splitter()
    splitter.push("Trailing text")
    splitter.flush()

    assert splitter.flush() == []


def test_config_defaults_keep_a_short_reply_in_one_chunk() -> None:
    # 40/240 are the shipped defaults: a two-sentence answer this short should
    # cost one request, not two.
    splitter = SentenceSplitter(min_chars=40, max_chars=240)
    assert len(splitter.push("The kitchen light is now on. Anything else?")) == 1


def test_min_chars_above_max_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_chars"):
        SentenceSplitter(min_chars=300, max_chars=100)


def test_a_non_positive_min_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_chars"):
        SentenceSplitter(min_chars=0, max_chars=100)


# --- OpenAISynthesizer ------------------------------------------------------


class FakeStreamingResponse:
    """Async context manager replaying scripted byte chunks."""

    def __init__(self, chunks: list[bytes] | None, error: Exception | None) -> None:
        self._chunks = chunks or []
        self._error = error

    async def __aenter__(self) -> Self:
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class FakeSpeech:
    """Records speech requests and hands back one scripted response."""

    def __init__(self, response: FakeStreamingResponse) -> None:
        self.requests: list[dict[str, Any]] = []
        self._response = response
        self.with_streaming_response = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> FakeStreamingResponse:
        self.requests.append(kwargs)
        return self._response


class FakeClient:
    """Stand-in for `AsyncOpenAI` that also captures constructor arguments."""

    def __init__(self, response: FakeStreamingResponse, **init_kwargs: Any) -> None:
        self.init_kwargs = init_kwargs
        self.speech = FakeSpeech(response)
        self.audio = SimpleNamespace(speech=self.speech)


BuildSynthesizer = Callable[..., tuple[OpenAISynthesizer, FakeClient]]


@pytest.fixture
def build_synthesizer(monkeypatch: pytest.MonkeyPatch) -> BuildSynthesizer:
    """Build a synthesizer whose provider call is faked, mirroring `patch_llm`."""

    def _build(
        chunks: list[bytes] | None = None,
        error: Exception | None = None,
    ) -> tuple[OpenAISynthesizer, FakeClient]:
        built: list[FakeClient] = []

        def _make_client(**kwargs: Any) -> FakeClient:
            response = FakeStreamingResponse(chunks, error)
            instance = FakeClient(response, **kwargs)
            built.append(instance)
            return instance

        monkeypatch.setattr("app.voice.tts.AsyncOpenAI", _make_client)
        synthesizer = OpenAISynthesizer(
            model=MODEL_NAME,
            base_url=BASE_URL,
            api_key="sk-test",  # pragma: allowlist secret
            instructions=INSTRUCTIONS,
            timeout_seconds=5.0,
        )
        return synthesizer, built[0]

    return _build


async def _drain(synthesizer: OpenAISynthesizer) -> list[bytes]:
    """Collect the stream, keeping the chunk boundaries for assertions."""
    return [chunk async for chunk in synthesizer.synthesize("Hello.", VOICE)]


async def test_synthesize_streams_the_provider_chunks(
    build_synthesizer: BuildSynthesizer,
) -> None:
    synthesizer, _ = build_synthesizer([b"\x01\x02", b"\x03\x04"])
    assert await _drain(synthesizer) == [b"\x01\x02", b"\x03\x04"]


async def test_synthesize_requests_raw_pcm_with_the_given_voice(
    build_synthesizer: BuildSynthesizer,
) -> None:
    synthesizer, client = build_synthesizer([b"\x00\x00"])
    await _drain(synthesizer)

    request = client.speech.requests[0]
    assert request["model"] == MODEL_NAME
    assert request["voice"] == VOICE
    assert request["input"] == "Hello."
    assert request["instructions"] == INSTRUCTIONS
    # A container would need demuxing, and this image has no ffmpeg.
    assert request["response_format"] == "pcm"


async def test_synthesize_carries_the_credentials_into_the_client(
    build_synthesizer: BuildSynthesizer,
) -> None:
    _, client = build_synthesizer([b"\x00\x00"])

    assert client.init_kwargs["base_url"] == BASE_URL
    assert client.init_kwargs["max_retries"] == 1


async def test_synthesize_realigns_a_chunk_split_mid_sample(
    build_synthesizer: BuildSynthesizer,
) -> None:
    # The transport may split at any byte. 0x01 on its own is half a sample, so
    # it must be held until 0x02 completes the pair rather than emitted alone —
    # an odd chunk would shift every later sample and play as noise. The total
    # length is even here, so realignment must lose nothing.
    synthesizer, _ = build_synthesizer([b"\x01", b"\x02", b"\x03\x04"])
    chunks = await _drain(synthesizer)

    assert [len(chunk) % 2 for chunk in chunks] == [0, 0]
    assert b"".join(chunks) == b"\x01\x02\x03\x04"


async def test_synthesize_drops_a_truncated_trailing_byte(
    build_synthesizer: BuildSynthesizer,
) -> None:
    synthesizer, _ = build_synthesizer([b"\x01\x02", b"\x03"])
    assert b"".join(await _drain(synthesizer)) == b"\x01\x02"


async def test_synthesize_propagates_a_provider_error(
    build_synthesizer: BuildSynthesizer,
) -> None:
    # Unlike the transcriber this streams, so the caller is the boundary.
    synthesizer, _ = build_synthesizer(None, RuntimeError("quota exceeded"))

    with pytest.raises(RuntimeError, match="quota exceeded"):
        await _drain(synthesizer)
