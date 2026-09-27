"""Tests for the voice doubles, so a broken double cannot manufacture a pass."""

import pytest
from conftest import FakeSynthesizer, FakeTranscriber, ScriptedVad, transcript


async def test_fake_transcriber_returns_its_script_in_order() -> None:
    fake = FakeTranscriber(transcript("eins"), transcript("two"))

    first = await fake.transcribe(b"\x00\x00", "de")
    second = await fake.transcribe(b"\x01\x01")

    assert (first.text, second.text) == ("eins", "two")
    assert fake.calls == [(b"\x00\x00", "de"), (b"\x01\x01", None)]


async def test_fake_transcriber_raises_when_its_script_runs_out() -> None:
    fake = FakeTranscriber()

    with pytest.raises(AssertionError, match="ran out of scripted results"):
        await fake.transcribe(b"\x00\x00")


async def test_fake_transcriber_raises_a_scripted_error() -> None:
    fake = FakeTranscriber(RuntimeError("provider exploded"))

    with pytest.raises(RuntimeError, match="provider exploded"):
        await fake.transcribe(b"\x00\x00")


def test_transcript_builds_a_successful_result() -> None:
    result = transcript("hello", language_hint="en")

    assert result.ok
    assert not result.is_empty
    assert result.provider == "fake"


async def test_fake_synthesizer_streams_the_same_pcm_for_every_sentence() -> None:
    fake = FakeSynthesizer(b"\x01\x02", b"\x03\x04")

    first = [chunk async for chunk in fake.synthesize("one", "marin")]
    second = [chunk async for chunk in fake.synthesize("two", "marin")]

    assert first == second == [b"\x01\x02", b"\x03\x04"]
    assert fake.requests == [("one", "marin"), ("two", "marin")]


async def test_fake_synthesizer_fails_mid_stream_not_at_call_time() -> None:
    # A real provider reports a failure from the stream, so a session that only
    # guards the call would still crash.
    fake = FakeSynthesizer(b"\x01\x02", raise_on=2)

    stream = fake.synthesize("first", "marin")
    assert [chunk async for chunk in stream] == [b"\x01\x02"]

    with pytest.raises(RuntimeError, match="told to fail on this call"):
        async for _ in fake.synthesize("second", "marin"):
            pass


def test_scripted_vad_returns_probabilities_in_order() -> None:
    vad = ScriptedVad().feed(0.1, 0.9)

    assert vad.speech_probability(b"a") == 0.1
    assert vad.speech_probability(b"b") == 0.9
    assert vad.windows == [b"a", b"b"]


def test_scripted_vad_raises_when_its_script_runs_out() -> None:
    with pytest.raises(AssertionError, match="ran out of scripted probabilities"):
        ScriptedVad().speech_probability(b"a")


def test_scripted_vad_reset_keeps_the_script() -> None:
    # The script is the audio stream; a detector reset must not rewind it.
    vad = ScriptedVad(0.5)

    vad.reset()

    assert vad.resets == 1
    assert vad.speech_probability(b"a") == 0.5


def test_fake_vad_fixture_starts_clean(fake_vad: ScriptedVad) -> None:
    assert fake_vad.windows == []
    assert fake_vad.resets == 0
