"""Tests for the /ws/voice route: handshake policy, framing, slot accounting,
idle timeout and rate limiting.

The real app is booted through the lifespan, exactly as test_api.py does, and only
the graph and the voice providers are swapped for fakes — so what is under test
is the route's own policy plus the real session behind it.
"""

import json
import time
from typing import Any

import pytest
from conftest import FakeSynthesizer, FakeTranscriber, ScriptedVad, transcript
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessageChunk

from app.api.ratelimit import SlidingWindowLimiter
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
