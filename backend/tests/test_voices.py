"""Tests for per-language text-to-speech voice selection."""

import pytest

from app.core.config import settings
from app.language.detector import DEFAULT_LANG, SUPPORTED_LANGS
from app.language.voices import voice_for

VOICES = settings.voice_tts_voices


def test_every_supported_language_has_a_configured_voice() -> None:
    assert set(SUPPORTED_LANGS) <= set(VOICES)


@pytest.mark.parametrize(("lang", "expected"), sorted(VOICES.items()))
def test_a_known_language_maps_to_its_configured_voice(
    lang: str,
    expected: str,
) -> None:
    assert voice_for(lang) == expected


def test_languages_do_not_share_a_voice() -> None:
    # Invisible in the assertions above, but a duplicate value means one
    # language answers in another language's voice.
    assert len(set(VOICES.values())) == len(VOICES)


@pytest.mark.parametrize("lang", [None, "", "fr", "xx-YY"])
def test_an_unknown_language_falls_back_to_the_default_voice(lang: str | None) -> None:
    assert voice_for(lang) == VOICES[DEFAULT_LANG]


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("pt", VOICES["pt-BR"]),
        ("PT-BR", VOICES["pt-BR"]),
        ("en-GB", VOICES["en"]),
    ],
)
def test_a_regional_variant_uses_its_base_language_voice(
    lang: str,
    expected: str,
) -> None:
    assert voice_for(lang) == expected


def test_an_operator_override_changes_the_voice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The configured table is the source of truth, so an override has to change
    # the choice — the module-level table this replaced silently ignored it.
    monkeypatch.setitem(settings.voice_tts_voices, "en", "override-voice")

    assert voice_for("en") == "override-voice"
