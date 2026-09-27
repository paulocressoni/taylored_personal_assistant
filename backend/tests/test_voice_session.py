"""Tests for the voice session turn loop.

The session is driven through the REAL `EndpointDetector` with a scripted VAD, so
the utterance boundary and barge-in are exercised rather than stubbed. Only the
VAD timings change: one silent window ends a segment instead of the sixteen that
the production default of 500 ms implies.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable
from typing import Any

import pytest
from conftest import FakeSynthesizer, FakeTranscriber, ScriptedVad, transcript
from langchain_core.messages import AIMessageChunk

from app.core.config import settings
from app.voice.audio import resample_pcm
from app.voice.session import VoiceSession
from app.voice.stt import Transcript
from app.voice.tts import PCM_SAMPLE_RATE
from app.voice.vad import WINDOW_BYTES

# Four 32 ms windows: two loud enough to open a segment, two quiet enough to
# close it. The contents are irrelevant — the scripted VAD ignores the audio.
UTTERANCE_PCM = bytes(WINDOW_BYTES * 4)
ONE_WINDOW = bytes(WINDOW_BYTES)
# One probability per window; two segments' worth by default.
VAD_SCRIPT = (0.9, 0.9, 0.1, 0.1) * 2
REPLY = "The kitchen light is now switched on for you."


class Recorder:
    """Async `send` that remembers every frame and can wait for frame counts."""

    def __init__(self) -> None:
        self.frames: list[bytes | dict[str, Any]] = []
        self._counts: dict[str, int] = {}
        self._events: dict[str, asyncio.Event] = {}

    async def __call__(self, frame: bytes | dict[str, Any]) -> None:
        self.frames.append(frame)
        if isinstance(frame, dict):
            kind = frame["type"]
            self._counts[kind] = self._counts.get(kind, 0) + 1
            event = self._events.get(kind)
            if event is not None:
                event.set()

    async def until(self, frame_type: str, count: int = 1) -> None:
        """Block until `count` frames of this type have been sent.

        Counted rather than a single event because a two-turn test has to wait for
        the SECOND answer to finish: the first one's event says nothing about it.
        """
        while self._counts.get(frame_type, 0) < count:
            event = asyncio.Event()
            self._events[frame_type] = event
            await event.wait()

    @property
    def control(self) -> list[dict[str, Any]]:
        """The control frames, in order."""
        return [frame for frame in self.frames if isinstance(frame, dict)]

    @property
    def types(self) -> list[str]:
        """The control frame types, in order."""
        return [frame["type"] for frame in self.control]

    @property
    def audio(self) -> bytes:
        """Every audio frame, concatenated."""
        return b"".join(frame for frame in self.frames if isinstance(frame, bytes))


class FakeGraph:
    """Duck-typed graph: replays scripted events and records its input state."""

    def __init__(self, *events: dict[str, Any]) -> None:
        self._events = events
        self.initial: list[dict[str, Any]] = []

    async def astream_events(
        self,
        initial: dict[str, Any],
        config: dict[str, Any] | None = None,
        version: str | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Record the seeded state, then yield the scripted events."""
        self.initial.append(initial)
        for event in self._events:
            yield event


class GatedTranscriber:
    """Transcriber that blocks until the test releases it, for barge-in."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[tuple[bytes, str | None]] = []

    async def transcribe(
        self, pcm: bytes, language_hint: str | None = None
    ) -> Transcript:
        """Announce the call, then wait to be released."""
        self.calls.append((pcm, language_hint))
        self.started.set()
        await self.release.wait()
        return transcript("turn on the light")


class AnnouncingTranscriber(FakeTranscriber):
    """`FakeTranscriber` that also signals when it is entered."""

    def __init__(self, *results: Transcript | Exception) -> None:
        super().__init__(*results)
        self.entered = asyncio.Event()

    async def transcribe(
        self, pcm: bytes, language_hint: str | None = None
    ) -> Transcript:
        """Signal, then behave like the fake."""
        self.entered.set()
        return await super().transcribe(pcm, language_hint)


def _lang_event(lang: str) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "detect_lang",
        "data": {"output": {"lang": lang}},
        "parent_ids": ["root"],
    }


def _token_event(text: str, node: str = "responder") -> dict[str, Any]:
    return {
        "event": "on_chat_model_stream",
        "name": "ChatDeepSeek",
        "data": {"chunk": AIMessageChunk(content=text)},
        "metadata": {"langgraph_node": node},
        "parent_ids": ["root"],
    }


def _end_event(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "on_chain_end",
        "name": "LangGraph",
        "data": {"output": state},
        "parent_ids": [],
    }


def _graph(*events: dict[str, Any]) -> FakeGraph:
    """A graph whose reply is `REPLY` in English."""
    return FakeGraph(_lang_event("en"), _token_event(REPLY), _end_event({}))


def _session(
    graph: FakeGraph,
    *,
    transcriber: Any | None = None,
    synthesizer: Any | None = None,
    vad_script: tuple[float, ...] = VAD_SCRIPT,
    output_sample_rate: int = PCM_SAMPLE_RATE,
) -> tuple[VoiceSession, Recorder]:
    """Build a session wired to fakes, and the recorder collecting its frames."""
    recorder = Recorder()
    session = VoiceSession(
        graph=graph,
        transcriber=transcriber or FakeTranscriber(transcript("turn on the light")),
        synthesizer=synthesizer or FakeSynthesizer(b"\x01\x00"),
        vad=ScriptedVad(*vad_script),
        send=recorder,
        session_id="voice-1",
        output_sample_rate=output_sample_rate,
    )
    return session, recorder


async def _drive(
    session: VoiceSession,
    *frames: bytes | dict[str, Any],
    until: Awaitable[None] | None = None,
) -> None:
    """Feed frames, optionally wait for the turn to finish, then stop.

    `until` is what makes these tests deterministic: the stop frame cancels the
    turn, so a test that wants to observe a turn's effects has to wait for it.
    """

    async def stream() -> AsyncIterator[bytes | dict[str, Any]]:
        for frame in frames:
            yield frame
        if until is not None:
            await until
        yield {"type": "stop"}

    await asyncio.wait_for(session.run(stream()), timeout=2)


async def _drive_turns(
    session: VoiceSession,
    *turns: bytes | dict[str, Any],
    recorder: Recorder,
) -> None:
    """Feed one utterance per turn, waiting for each answer before the next.

    Utterances pushed back to back are NOT two turns: the second onset arrives
    while the first answer is still playing, so it is a barge-in and cancels that
    turn. A test about sequential turns therefore has to wait between them.
    """

    async def stream() -> AsyncIterator[bytes | dict[str, Any]]:
        for index, frame in enumerate(turns, start=1):
            yield frame
            await recorder.until("audio_end", index)
        yield {"type": "stop"}

    await asyncio.wait_for(session.run(stream()), timeout=2)


@pytest.fixture(autouse=True)
def fast_endpointing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close a segment after one silent window instead of sixteen.

    The production default is 500 ms of silence, which would turn every test here
    into a VAD rehearsal rather than a session test.
    """
    monkeypatch.setattr(settings, "voice_vad_min_silence_ms", 32)
    monkeypatch.setattr(settings, "voice_vad_min_speech_ms", 32)


async def test_the_session_announces_the_negotiated_rates() -> None:
    session, recorder = _session(_graph())

    await _drive(session)

    assert recorder.control[0] == {
        "type": "ready",
        "input_sample_rate": settings.voice_input_sample_rate,
        "output_sample_rate": PCM_SAMPLE_RATE,
        "format": "pcm_s16le",
    }


async def test_an_utterance_is_transcribed_answered_and_spoken() -> None:
    transcriber = FakeTranscriber(transcript("turn on the light"))
    session, recorder = _session(_graph(), transcriber=transcriber)

    await _drive(session, UTTERANCE_PCM, until=recorder.until("audio_end"))

    # Onset is reported before the segment closes — that is what lets the client
    # stop playing the instant somebody speaks.
    assert recorder.types == [
        "ready",
        "speech_start",
        "transcript",
        "token",
        "audio_end",
    ]
    assert recorder.control[2]["text"] == "turn on the light"
    assert recorder.control[3]["content"] == REPLY
    assert recorder.audio == b"\x01\x00"
    # Trailing silence is trimmed before transcription, so the audio sent is
    # shorter than the windows that produced it.
    assert 0 < len(transcriber.calls[0][0]) < len(UTTERANCE_PCM)


async def test_only_the_responder_tokens_reach_the_client() -> None:
    graph = FakeGraph(
        _lang_event("en"),
        _token_event("<route>knowledge</route>", node="router"),
        _token_event(REPLY),
        _end_event({}),
    )
    session, recorder = _session(graph)

    await _drive(session, UTTERANCE_PCM, until=recorder.until("audio_end"))

    spoken = [
        frame["content"] for frame in recorder.control if frame["type"] == "token"
    ]
    assert spoken == [REPLY]


async def test_each_sentence_is_synthesized_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_tts_min_chunk_chars", 8)
    graph = FakeGraph(
        _lang_event("en"),
        _token_event("Hello there. How are you?"),
        _end_event({}),
    )
    synthesizer = FakeSynthesizer(b"\x01\x00")
    session, recorder = _session(graph, synthesizer=synthesizer)

    await _drive(session, UTTERANCE_PCM, until=recorder.until("audio_end"))

    assert [text for text, _ in synthesizer.requests] == [
        "Hello there.",
        "How are you?",
    ]
    assert recorder.audio == b"\x01\x00\x01\x00"


async def test_the_resolved_language_picks_the_voice() -> None:
    graph = FakeGraph(_lang_event("de"), _token_event(REPLY), _end_event({}))
    synthesizer = FakeSynthesizer(b"\x01\x00")
    session, recorder = _session(graph, synthesizer=synthesizer)

    await _drive(session, UTTERANCE_PCM, until=recorder.until("audio_end"))

    assert synthesizer.requests[0][1] == settings.voice_tts_voices["de"]


async def test_the_previous_turn_language_biases_the_next_transcription() -> None:
    graph = FakeGraph(
        _lang_event("de"),
        _token_event(REPLY),
        _end_event({}),
        _lang_event("de"),
        _token_event(REPLY),
        _end_event({}),
    )
    transcriber = FakeTranscriber(transcript("hallo"), transcript("hallo"))
    session, recorder = _session(graph, transcriber=transcriber)

    await _drive_turns(session, UTTERANCE_PCM, UTTERANCE_PCM, recorder=recorder)

    assert transcriber.calls[0][1] is None  # nothing resolved yet
    assert transcriber.calls[1][1] == "de"
    assert graph.initial[1]["stt_lang"] == "de"
    assert graph.initial[1]["channel"] == "voice"


async def test_hinting_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "voice_stt_language_hint", False)
    transcriber = FakeTranscriber(transcript("hallo"), transcript("hallo"))
    graph = FakeGraph(
        _lang_event("de"),
        _token_event(REPLY),
        _end_event({}),
        _lang_event("de"),
        _token_event(REPLY),
        _end_event({}),
    )
    session, recorder = _session(graph, transcriber=transcriber)

    await _drive_turns(session, UTTERANCE_PCM, UTTERANCE_PCM, recorder=recorder)

    assert [hint for _, hint in transcriber.calls] == [None, None]


async def test_a_failed_transcription_reports_an_error_and_stops() -> None:
    failed = Transcript(
        text="", language_hint=None, duration_s=1.0, provider="fake", error="boom"
    )
    session, recorder = _session(_graph(), transcriber=FakeTranscriber(failed))

    await _drive(session, UTTERANCE_PCM, until=recorder.until("error"))

    assert recorder.types == ["ready", "speech_start", "error"]
    assert recorder.audio == b""


async def test_an_empty_transcript_sends_nothing() -> None:
    transcriber = AnnouncingTranscriber(transcript(""))
    session, recorder = _session(_graph(), transcriber=transcriber)

    async def stream() -> AsyncIterator[bytes | dict[str, Any]]:
        yield UTTERANCE_PCM
        # Nothing follows an empty transcript, so there is no frame to wait for;
        # the turn simply must not emit one.
        await transcriber.entered.wait()
        yield {"type": "stop"}

    await asyncio.wait_for(session.run(stream()), timeout=2)

    assert len(transcriber.calls) == 1
    assert transcriber.calls[0][0]  # the trimmed segment carried audio
    assert transcriber.calls[0][1] is None
    # `speech_start` still went out: the onset is reported as soon as audio
    # crosses the threshold, before anything knows whether it becomes a turn.
    assert recorder.types == ["ready", "speech_start"]


async def test_a_silent_blip_never_starts_a_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_vad_min_speech_ms", 200)
    transcriber = FakeTranscriber(transcript("nobody said this"))
    session, recorder = _session(_graph(), transcriber=transcriber)
    # One loud window is 32 ms of speech, well under the 200 ms floor.
    await _drive(session, UTTERANCE_PCM)

    assert transcriber.calls == []
    # The onset frame still went out before the blip was confirmed: holding it
    # back until confirmation would delay interrupting playback.
    assert recorder.types == ["ready", "speech_start"]


async def test_speech_start_cuts_off_the_answer_in_flight() -> None:
    transcriber = GatedTranscriber()
    session, recorder = _session(
        _graph(),
        transcriber=transcriber,
        # A fifth loud window after the first segment: an onset with no segment
        # behind it to become a second turn.
        vad_script=(0.9, 0.9, 0.1, 0.1, 0.9),
    )

    async def stream() -> AsyncIterator[bytes | dict[str, Any]]:
        yield UTTERANCE_PCM
        await transcriber.started.wait()
        yield ONE_WINDOW
        yield {"type": "stop"}

    await asyncio.wait_for(session.run(stream()), timeout=2)

    # Two onsets: the one that opened the first segment, and the one that cut the
    # stuck answer off.
    assert recorder.types == ["ready", "speech_start", "speech_start"]
    assert len(transcriber.calls) == 1
    assert recorder.audio == b""


async def test_a_synthesizer_failure_is_reported_and_the_socket_survives() -> None:
    synthesizer = FakeSynthesizer(b"\x01\x00", raise_on=1)
    session, recorder = _session(_graph(), synthesizer=synthesizer)

    await _drive(session, UTTERANCE_PCM, until=recorder.until("error"))

    assert recorder.types == [
        "ready",
        "speech_start",
        "transcript",
        "token",
        "error",
    ]
    assert recorder.audio == b""


async def test_a_16khz_client_gets_the_resampled_audio() -> None:
    payload = bytes(2400)  # 1200 samples at the TTS-native 24 kHz
    synthesizer = FakeSynthesizer(payload)
    session, recorder = _session(
        _graph(), synthesizer=synthesizer, output_sample_rate=16000
    )

    await _drive(session, UTTERANCE_PCM, until=recorder.until("audio_end"))

    assert recorder.audio == resample_pcm(payload, PCM_SAMPLE_RATE, 16000)


def test_an_unsupported_output_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="output_sample_rate"):
        _session(_graph(), output_sample_rate=44100)
