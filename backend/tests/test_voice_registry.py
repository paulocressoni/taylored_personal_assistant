"""Tests for the process-wide voice wiring."""

import pytest
from pydantic import SecretStr

from app.core.config import settings
from app.voice.registry import SessionSlots, build_voice_providers
from app.voice.stt import OpenAICompatibleTranscriber
from app.voice.tts import OpenAISynthesizer
from app.voice.vad import SileroVad


def _voice_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch voice on with keys that look real enough to pass the checks."""
    monkeypatch.setattr(settings, "voice_enabled", True)
    monkeypatch.setattr(settings, "voice_stt_api_key", SecretStr("test-stt-key"))
    monkeypatch.setattr(settings, "voice_tts_api_key", SecretStr("test-tts-key"))


def test_voice_off_builds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Set explicitly rather than inherited: a developer's .env may well have
    # voice switched on.
    monkeypatch.setattr(settings, "voice_enabled", False)

    assert build_voice_providers() is None


def test_a_missing_key_builds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _voice_on(monkeypatch)
    monkeypatch.setattr(settings, "voice_tts_api_key", SecretStr(""))

    assert build_voice_providers() is None


def test_the_providers_are_the_real_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    _voice_on(monkeypatch)

    providers = build_voice_providers()

    assert providers is not None
    assert isinstance(providers.transcriber, OpenAICompatibleTranscriber)
    assert isinstance(providers.synthesizer, OpenAISynthesizer)


def test_the_transcriber_is_built_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _voice_on(monkeypatch)
    seen: dict[str, object] = {}

    class SpyTranscriber:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(
        "app.voice.registry.OpenAICompatibleTranscriber", SpyTranscriber
    )
    monkeypatch.setattr(settings, "voice_stt_model", "test-whisper")

    providers = build_voice_providers()

    assert providers is not None
    assert isinstance(providers.transcriber, SpyTranscriber)
    assert seen["model"] == "test-whisper"
    assert seen["api_key"] == "test-stt-key"  # pragma: allowlist secret
    assert seen["sample_rate"] == settings.voice_input_sample_rate


def test_the_synthesizer_is_built_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _voice_on(monkeypatch)
    seen: dict[str, object] = {}

    class SpySynthesizer:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr("app.voice.registry.OpenAISynthesizer", SpySynthesizer)
    monkeypatch.setattr(settings, "voice_tts_instructions", "Speak like a butler.")

    providers = build_voice_providers()

    assert providers is not None
    assert isinstance(providers.synthesizer, SpySynthesizer)
    assert seen["instructions"] == "Speak like a butler."
    assert seen["api_key"] == "test-tts-key"  # pragma: allowlist secret


def test_each_socket_gets_its_own_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    _voice_on(monkeypatch)

    providers = build_voice_providers()

    assert providers is not None
    first = providers.vad_factory()
    second = providers.vad_factory()
    assert isinstance(first, SileroVad)
    # Correctness, not parsimony: a shared VAD would fold two conversations'
    # audio into one recurrent state.
    assert first is not second


def test_slots_refuse_more_than_the_cap() -> None:
    slots = SessionSlots(2)

    assert slots.acquire() is True
    assert slots.acquire() is True
    assert slots.acquire() is False
    assert slots.active == 2


def test_releasing_a_slot_lets_the_next_socket_in() -> None:
    slots = SessionSlots(1)
    assert slots.acquire() is True

    slots.release()

    assert slots.active == 0
    assert slots.acquire() is True


def test_releasing_without_holding_never_goes_negative() -> None:
    slots = SessionSlots(1)

    slots.release()

    assert slots.active == 0
