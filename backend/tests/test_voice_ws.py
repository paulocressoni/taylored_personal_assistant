"""Tests for the /ws/voice route: handshake policy, framing, slot accounting,
idle timeout and rate limiting.

The real app is booted through the lifespan, exactly as test_api.py does, and only
the graph and the voice providers are swapped for fakes — so what is under test
is the route's own policy plus the real session behind it.
"""

import asyncio
import json
import logging
import time
from types import SimpleNamespace
from typing import Any

import pytest
from conftest import (
    BrokenWake,
    FakeSynthesizer,
    FakeTranscriber,
    ScriptedVad,
    ScriptedWake,
    transcript,
)
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk
from starlette.websockets import WebSocketState

from app.api.ratelimit import SlidingWindowLimiter
from app.api.routes import _close_quietly, _serve_voice
from app.api.schemas import VoiceStartRequest
from app.core.config import settings
from app.main import app
from app.voice.registry import SessionSlots, VoiceProviders
from app.voice.session import OUTPUT_SAMPLE_RATES
from app.voice.vad import WINDOW_BYTES

API_KEY = settings.assistant_api_key.get_secret_value()
UTTERANCE_PCM = bytes(WINDOW_BYTES * 4)
START = {"session_id": "voice-1", "output_sample_rate": 24000}


class FakeGraph:
    """Duck-typed graph: one English reply, plus a record of its input state."""

    def __init__(self) -> None:
        self.initial: list[dict[str, Any]] = []

    async def astream_events(self, initial, config=None, version="v2"):
        self.initial.append(initial)
        yield {
            "event": "on_chat_model_stream",
            "metadata": {"langgraph_node": "responder"},
            "data": {"chunk": AIMessageChunk(content="The light is on.")},
        }
        yield {
            "event": "on_chain_end",
            "name": "LangGraph",
            "parent_ids": [],
            "data": {"output": {"lang": "en"}},
        }


def _providers(*probabilities: float) -> VoiceProviders:
    """Providers whose only real part is the scripted VAD."""
    return VoiceProviders(
        transcriber=FakeTranscriber(transcript("turn on the light")),
        synthesizer=FakeSynthesizer(b"\x01\x00"),
        vad_factory=lambda: ScriptedVad(*probabilities),
    )


def _wait_for_release(slots: SessionSlots) -> None:
    """Wait briefly for the route's `finally` to give the slot back.

    The socket's close is processed by the app's own task, which can be a moment
    behind the test's thread, so this polls instead of assuming.
    """
    deadline = time.monotonic() + 1.0
    while slots.active and time.monotonic() < deadline:
        time.sleep(0.01)


@pytest.fixture
def fake_graph() -> FakeGraph:
    """The graph the `client` fixture installs, exposed for input assertions."""
    return FakeGraph()


@pytest.fixture
def client(fake_graph: FakeGraph):
    """Boot the app and swap the graph and the voice providers for fakes."""
    with TestClient(app) as test_client:
        app.state.graph = fake_graph
        app.state.voice_providers = _providers(0.9, 0.9, 0.1, 0.1)
        app.state.voice_slots = SessionSlots(1)
        yield test_client


@pytest.fixture
def fast_endpointing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close a segment after one silent window instead of sixteen."""
    monkeypatch.setattr(settings, "voice_vad_min_silence_ms", 32)
    monkeypatch.setattr(settings, "voice_vad_min_speech_ms", 32)


def test_the_api_accepts_only_the_rates_the_session_supports() -> None:
    # The schema spells the rates out for the OpenAPI docs; this is what keeps
    # that list and the session's own allowlist from drifting apart.
    accepted = VoiceStartRequest.model_fields["output_sample_rate"]

    assert set(accepted.annotation.__args__) == set(OUTPUT_SAMPLE_RATES)


def test_an_unknown_key_is_refused(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice") as socket:
        assert socket.receive_json()["detail"] == "unauthorized"
        assert socket.receive()["code"] == 1008


def test_voice_is_refused_when_the_feature_is_off(client: TestClient) -> None:
    app.state.voice_providers = None

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        assert socket.receive_json()["detail"] == "voice is not enabled"
        assert socket.receive()["code"] == 1008


def test_a_busy_slot_cap_refuses_a_new_socket(client: TestClient) -> None:
    app.state.voice_slots = SessionSlots(0)

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        assert socket.receive_json()["detail"] == "too many voice sessions"
        assert socket.receive()["code"] == 1013


def test_a_malformed_start_frame_is_rejected(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps({"session_id": 5}))

        assert socket.receive_json()["type"] == "error"
        assert socket.receive()["code"] == 1003


def test_an_unsupported_output_rate_is_rejected(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(
            json.dumps({"session_id": "voice-1", "output_sample_rate": 44100})
        )

        assert socket.receive_json()["type"] == "error"
        assert socket.receive()["code"] == 1003


def test_the_ready_frame_announces_the_negotiated_rate(client: TestClient) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps(START))

        assert socket.receive_json() == {
            "type": "ready",
            "input_sample_rate": settings.voice_input_sample_rate,
            "output_sample_rate": 24000,
            "format": "pcm_s16le",
            "wake_word": False,
        }


def test_a_spoken_turn_round_trips_over_the_socket(
    client: TestClient,
    fast_endpointing: None,
) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps(START))
        assert socket.receive_json()["type"] == "ready"

        socket.send_bytes(UTTERANCE_PCM)

        assert socket.receive_json()["type"] == "speech_start"
        assert socket.receive_json() == {
            "type": "transcript",
            "text": "turn on the light",
        }
        assert socket.receive_json() == {
            "type": "token",
            "content": "The light is on.",
        }
        assert socket.receive_bytes() == b"\x01\x00"
        assert socket.receive_json()["type"] == "audio_end"


def test_the_slot_is_held_while_open_and_released_on_close(
    client: TestClient,
) -> None:
    slots = SessionSlots(1)
    app.state.voice_slots = slots

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps(START))
        assert socket.receive_json()["type"] == "ready"
        assert slots.active == 1

    _wait_for_release(slots)
    assert slots.active == 0


def test_a_spoken_turn_reaches_the_graph_as_a_voice_turn(
    client: TestClient,
    fake_graph: FakeGraph,
    fast_endpointing: None,
) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps({**START, "device_id": "kitchen"}))
        assert socket.receive_json()["type"] == "ready"

        socket.send_bytes(UTTERANCE_PCM)

        assert socket.receive_json()["type"] == "speech_start"
        assert socket.receive_json()["type"] == "transcript"
        assert socket.receive_json()["type"] == "token"
        socket.receive_bytes()  # the spoken answer
        assert socket.receive_json()["type"] == "audio_end"

    # Seeding the turn is the route's whole job, and `channel` is what selects
    # the spoken-answer prompt downstream and tags the trace.
    (seeded,) = fake_graph.initial
    assert seeded["channel"] == "voice"
    assert seeded["user_input"] == "turn on the light"
    assert seeded["session_id"] == "voice-1"
    assert seeded["device_id"] == "kitchen"
    assert seeded["stt_lang"] is None  # the first turn has nothing to bias it


def test_an_idle_socket_is_closed_and_its_slot_released(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_idle_timeout_seconds", 0.2)
    slots = SessionSlots(1)
    app.state.voice_slots = slots

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(json.dumps(START))
        assert socket.receive_json()["type"] == "ready"
        assert slots.active == 1

        # Nothing follows: no audio and no keepalive. The SERVER has to hang up,
        # because a vanished client would otherwise hold a slot — and a provider
        # quota — until the operating system notices. Asserting the close (rather
        # than only the release below) is what makes this a real test: leaving the
        # `with` block releases the slot whatever the route does.
        assert socket.receive()["type"] == "websocket.close"

    _wait_for_release(slots)
    assert slots.active == 0


def test_a_rate_limited_socket_is_refused_before_it_takes_a_slot(
    client: TestClient,
) -> None:
    app.state.rate_limiter = SlidingWindowLimiter(limit=1, window_seconds=60)

    # The one allowed connection spends the whole budget.
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as first:
        first.send_text(json.dumps(START))
        assert first.receive_json()["type"] == "ready"

    with (
        pytest.raises(WebSocketDisconnect) as exc_info,
        client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as second,
    ):
        assert second.receive_json()["detail"] == "rate limit exceeded"
        second.receive_json()  # surfaces the close as WebSocketDisconnect

    assert exc_info.value.code == 1013
    assert app.state.voice_slots.active == 0


# --- the wake-word start frame ----------------------------------------------


def _providers_with_wake(wake: Any, phrase: str = "hey jarvis") -> VoiceProviders:
    """Providers with a wake factory, so a session can actually be gated."""
    return VoiceProviders(
        transcriber=FakeTranscriber(transcript("Hey Jarvis, turn on the light")),
        synthesizer=FakeSynthesizer(b"\x01\x00"),
        vad_factory=lambda: ScriptedVad(0.9, 0.9, 0.1, 0.1),
        wake_word_factory=lambda: wake,
        wake_phrase=phrase,
    )


def _ready(socket: Any, start: dict[str, Any]) -> dict[str, Any]:
    """Send a start frame and return the ready frame it produced."""
    socket.send_text(json.dumps(start))
    return socket.receive_json()


@pytest.mark.parametrize(
    ("wake_word", "expected"),
    [
        ("auto", True),  # follows the server, which has a detector here
        ("on", True),
        ("manual", False),  # a user gesture is never gated
        ("off", False),  # allowed by default
    ],
)
def test_the_start_frame_decides_whether_the_session_is_gated(
    client: TestClient, wake_word: str, expected: bool
) -> None:
    # The four values are distinguished by the echo: a factory call count cannot
    # tell them apart, because the two un-gated values never call it.
    app.state.voice_providers = _providers_with_wake(ScriptedWake(0.0))

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        ready = _ready(
            socket,
            {
                "session_id": "wake-ws",
                "output_sample_rate": 24000,
                "wake_word": wake_word,
            },
        )

        assert ready["type"] == "ready"
        assert ready["wake_word"] is expected


def test_a_server_without_a_detector_echoes_an_ungated_session(
    client: TestClient,
) -> None:
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        ready = _ready(socket, {"session_id": "wake-ws-2", "output_sample_rate": 24000})

        assert ready["wake_word"] is False


def test_asking_for_the_wake_word_on_an_unconfigured_server_is_refused(
    client: TestClient,
) -> None:
    # Never silently un-gate: that would stream ambient audio to a paid STT while
    # the client believes it is gated.
    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(
            json.dumps(
                {
                    "session_id": "wake-ws-3",
                    "output_sample_rate": 24000,
                    "wake_word": "on",
                }
            )
        )

        assert socket.receive_json()["detail"] == (
            "wake word is not enabled on this server"
        )
        assert socket.receive()["code"] == 1008


def test_opting_out_is_refused_when_the_operator_forbids_it(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.state.voice_providers = _providers_with_wake(ScriptedWake(0.0))
    monkeypatch.setattr(settings, "voice_wake_word_allow_opt_out", False)

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        socket.send_text(
            json.dumps(
                {
                    "session_id": "wake-ws-4",
                    "output_sample_rate": 24000,
                    "wake_word": "off",
                }
            )
        )

        assert socket.receive_json()["detail"] == "this server requires the wake word"
        assert socket.receive()["code"] == 1008


def test_manual_sessions_survive_a_strict_server(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The browser must keep working when opt-out is forbidden; only an explicit
    # "off" is refused, because a click is not ambient audio.
    app.state.voice_providers = _providers_with_wake(ScriptedWake(0.0))
    monkeypatch.setattr(settings, "voice_wake_word_allow_opt_out", False)

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        ready = _ready(
            socket,
            {
                "session_id": "wake-ws-5",
                "output_sample_rate": 24000,
                "wake_word": "manual",
            },
        )

        assert ready["wake_word"] is False


def test_a_broken_detector_closes_the_socket_with_1011(client: TestClient) -> None:
    app.state.voice_providers = _providers_with_wake(BrokenWake())

    with client.websocket_connect("/ws/voice", subprotocols=[API_KEY]) as socket:
        ready = _ready(
            socket,
            {"session_id": "wake-ws-6", "output_sample_rate": 24000, "wake_word": "on"},
        )
        assert ready["wake_word"] is True

        socket.send_bytes(UTTERANCE_PCM)

        assert socket.receive_json()["detail"] == "wake word unavailable"
        assert socket.receive()["code"] == 1011


class VanishedPeer:
    """A socket whose peer dropped without sending a CLOSE frame.

    Models exactly what uvicorn does on the wire: `receive()` reports the
    disconnect, `application_state` stays CONNECTED because *we* never sent a
    close, and `close()` then fails — Starlette converts uvicorn's
    `ClientDisconnected` into `WebSocketDisconnect(1006)` inside `send()`.
    """

    def __init__(self, app_state: Any, messages: list[dict[str, Any]]) -> None:
        self.app = SimpleNamespace(state=app_state)
        self.application_state = WebSocketState.CONNECTED
        self._messages = messages
        self.closed: list[int | None] = []
        self.sent: list[dict[str, Any]] = []

    async def receive(self) -> dict[str, Any]:
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(30)
        raise AssertionError("receive() was called after the script ran out")

    async def send_json(self, frame: dict[str, Any]) -> None:
        self.sent.append(frame)

    async def send_bytes(self, frame: bytes) -> None:
        self.sent.append({"type": "binary", "size": len(frame)})

    async def close(self, code: int | None = None) -> None:
        self.closed.append(code)
        self.application_state = WebSocketState.DISCONNECTED
        raise WebSocketDisconnect(code=1006)


def _vanished_peer(session_messages: list[dict[str, Any]]) -> VanishedPeer:
    state = SimpleNamespace(graph=FakeGraph(), voice_providers=None, voice_slots=None)
    return VanishedPeer(
        state,
        [{"type": "websocket.receive", "text": json.dumps(START)}, *session_messages],
    )


async def test_a_vanished_client_ends_the_session_without_a_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The soak produced one "Exception in ASGI application" per closed session
    # because the close in the `finally` ran after the peer had gone. This is
    # that exact ordering: the loop ends normally, then close() fails.
    socket = _vanished_peer([{"type": "websocket.disconnect"}])

    with caplog.at_level(logging.INFO, logger="app.api.routes"):
        await _serve_voice(socket, _providers(0.9))

    reasons = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.api.routes" and "voice session closed:" in r.getMessage()
    ]
    assert reasons == ["voice session closed: client gone"]
    assert socket.closed == [None]


async def test_close_quietly_still_raises_an_unexpected_error() -> None:
    # The suppression is scoped to "the peer is gone": any other failure while
    # closing must surface, or a real bug would look like a normal hang-up.
    class Broken(VanishedPeer):
        async def close(self, code: int | None = None) -> None:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await _close_quietly(Broken(SimpleNamespace(), []))  # type: ignore[arg-type]


async def test_an_idle_socket_names_the_idle_timeout(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "voice_idle_timeout_seconds", 0.05)
    socket = _vanished_peer([])

    with caplog.at_level(logging.INFO, logger="app.api.routes"):
        await _serve_voice(socket, _providers(0.9))

    reasons = [
        r.getMessage()
        for r in caplog.records
        if r.name == "app.api.routes" and "voice session closed:" in r.getMessage()
    ]
    assert reasons == ["voice session closed: idle timeout"]


async def test_closing_an_already_closed_socket_is_not_an_error() -> None:
    # `main` raised RuntimeError('Cannot call "send" once a close message has
    # been sent.') after every graceful disconnect; the state guard covers it.
    socket = _vanished_peer([])
    socket.application_state = WebSocketState.DISCONNECTED

    await _close_quietly(socket)  # type: ignore[arg-type]

    assert socket.closed == []
