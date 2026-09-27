"""Tests for per-language text-to-speech voice selection."""

import pytest

from app.language.detector import DEFAULT_LANG, SUPPORTED_LANGS
from app.language.voices import (
    DEFAULT_VOICE,
    VOICE_BY_LANG,
    _check_coverage,
    voice_for,
)


def test_every_supported_language_has_a_voice() -> None:
    assert set(SUPPORTED_LANGS) <= set(VOICE_BY_LANG)


def test_the_default_voice_is_the_default_language_voice() -> None:
    assert DEFAULT_VOICE == VOICE_BY_LANG[DEFAULT_LANG]


@pytest.mark.parametrize(("lang", "expected"), sorted(VOICE_BY_LANG.items()))
def test_a_known_language_maps_to_its_own_voice(lang: str, expected: str) -> None:
    assert voice_for(lang) == expected


def test_languages_do_not_share_a_voice() -> None:
    # Invisible in the assertions above, but a duplicate value means one
    # language answers in another language's voice.
    assert len(set(VOICE_BY_LANG.values())) == len(VOICE_BY_LANG)


@pytest.mark.parametrize("lang", [None, "", "fr", "xx-YY"])
def test_an_unknown_language_falls_back_to_the_default_voice(
    lang: str | None,
) -> None:
    assert voice_for(lang) == DEFAULT_VOICE


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        ("pt", VOICE_BY_LANG["pt-BR"]),
        ("PT-BR", VOICE_BY_LANG["pt-BR"]),
        ("en-GB", VOICE_BY_LANG["en"]),
    ],
)
def test_a_regional_variant_uses_its_base_language_voice(
    lang: str,
    expected: str,
) -> None:
    assert voice_for(lang) == expected


def test_coverage_check_accepts_the_supported_languages() -> None:
    _check_coverage(SUPPORTED_LANGS)


def test_coverage_check_names_the_language_without_a_voice() -> None:
    with pytest.raises(RuntimeError, match="fr"):
        _check_coverage(["en", "fr"])
