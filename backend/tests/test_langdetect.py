"""Fast unit tests for app.language.detector — pure logic, no LLM, no network.

The whole point of M05: detection must be deterministic and testable offline.

NOTE: one test imports app.core.config to assert the detector's supported set
stays in sync with settings.supported_languages — that single test needs
ENV=dev (like test_config.py). The rest of this file is ENV-free.
"""

import pytest

from app.language.detector import (
    DEFAULT_LANG,
    MIN_CONFIDENCE,
    SUPPORTED_LANGS,
    detect_lang,
    resolve_lang,
)

# --- happy path: real detection on the DONE WHEN sentences -----------------


def test_detect_german() -> None:
    lang, conf = detect_lang("Wie ist das Wetter?")
    assert lang == "de"
    assert conf >= MIN_CONFIDENCE


def test_detect_portuguese() -> None:
    lang, conf = detect_lang("Que horas são?")
    assert lang == "pt-BR"
    assert conf >= MIN_CONFIDENCE


def test_detect_english() -> None:
    lang, conf = detect_lang("What is the weather like?")
    assert lang == "en"
    assert conf >= MIN_CONFIDENCE


def test_never_returns_unsupported_lang() -> None:
    # French is NOT in the candidate set; we must only ever get one of
    # en/de/pt-BR (or None) — never "fr" leaking into state["lang"].
    lang, _conf = detect_lang("Quelle heure est-il ?")
    assert lang is None or lang in SUPPORTED_LANGS


# --- impossible / empty input ---------------------------------------------


def test_empty_text_is_none() -> None:
    assert detect_lang("") == (None, 0.0)
    assert detect_lang("   ") == (None, 0.0)


# --- fallback chain -------------------------------------------------------


def test_empty_text_falls_back_through_chain() -> None:
    # 2nd rung: previous turn's language wins over preference (chain order).
    assert resolve_lang("", previous_lang="de", preference="pt-BR") == "de"
    # 3rd rung: preference when no previous turn.
    assert resolve_lang("", preference="pt-BR") == "pt-BR"
    # 4th rung: default.
    assert resolve_lang("") == DEFAULT_LANG


def test_low_confidence_uses_previous_lang(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force a low-confidence detection to exercise the policy without
    # depending on lingua's exact numbers for some ambiguous string.
    monkeypatch.setattr("app.language.detector.detect_lang", lambda text: ("en", 0.3))
    assert resolve_lang("gibberish", previous_lang="de") == "de"


def test_high_confidence_beats_previous_lang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.language.detector.detect_lang", lambda text: ("de", 0.95))
    assert resolve_lang("anything", previous_lang="en") == "de"


# --- config sync guard ----------------------------------------------------


def test_supported_langs_match_config() -> None:
    # Requires ENV=dev (see module docstring). Keeps two sources of truth honest.
    from app.core.config import settings

    assert SUPPORTED_LANGS == settings.supported_languages
