"""Deterministic language detection for the assistant's supported languages.

WHY A LIBRARY AND NOT THE LLM:
  - lingua is free, near-instant, deterministic and unit-testable: no API
    call, no token spend, no prompt drift, no "the LLM happened to reply in
    the right language this time".
  - The LLM only ever *replies in* a language; it is not a reliable
    *detector of* a language. Later milestones need ``lang`` as a structured
    value (TTS voice selection in M29, stored preference in M13).

Leaf module: imports nothing from the app, so it stays trivially unit-testable.
"""

from lingua import Language, LanguageDetectorBuilder

# Restricted candidate set = better accuracy on short text. The library's own
# docs: "the more languages take part in the decision process, the less
# accurate are the detection results." Must stay in sync with
# settings.supported_languages — test_langdetect.py guards this.
SUPPORTED_LANGS = ["en", "de", "pt-BR"]

DEFAULT_LANG = "en"

# Below this confidence, treat the top detection as "unreliable" and walk the
# fallback chain (previous turn -> stored preference -> DEFAULT_LANG).
# Tunable: raise for stricter behaviour, lower for more aggressive
# single-turn detection. Note short/ambiguous input yields lower confidence.
MIN_CONFIDENCE = 0.6

# lingua has one generic "Portuguese" model; we map it to our pt-BR tag.
_LINGUA_LANGS = [Language.ENGLISH, Language.GERMAN, Language.PORTUGUESE]

# Build ONE detector at import time. The library is thread-safe and shares
# language models across instances, so a module-level singleton is correct —
# do NOT rebuild it per request.
_detector = LanguageDetectorBuilder.from_languages(*_LINGUA_LANGS).build()


def _to_app_lang(language: Language) -> str | None:
    """Map a lingua Language to our app tag, or None if unmapped.

    Args:
        language (Language): The lingua Language to map.

    Returns:
        str | None: The corresponding app language tag, or None if unmapped.
    """
    if language == Language.ENGLISH:
        return "en"
    if language == Language.GERMAN:
        return "de"
    if language == Language.PORTUGUESE:
        return "pt-BR"
    return None


def detect_lang(text: str) -> tuple[str | None, float]:
    """Detect the language of the given text and return the app language tag and confidence.
    - Return ``(app language tag, confidence)`` for ``text``.
    - Returns ``(None, 0.0)`` when detection is impossible (empty/whitespace
        input or an unmapped result). Never raises.

    Args:
        text (str): The input text to detect the language of.

    Returns:
        tuple[str | None, float]: A tuple containing the detected app language tag and confidence.
    """
    if not text or not text.strip():
        return None, 0.0

    # compute_language_confidence_values returns every candidate language
    # sorted by confidence (0.0-1.0, summing to 1.0), so [0] is the winner.
    confidences = _detector.compute_language_confidence_values(text)
    if not confidences:
        return None, 0.0

    top = confidences[0]
    app_lang = _to_app_lang(top.language)
    if app_lang is None:
        return None, 0.0
    return app_lang, top.value


def resolve_lang(
    text: str,
    *,
    previous_lang: str | None = None,
    preference: str | None = None,
    min_confidence: float = MIN_CONFIDENCE,
) -> str:
    """Resolve this turn's language with the M05 fallback chain:

        1. detector confidence >= min_confidence  -> use the detection
        2. previous turn's language               -> keep the conversation's language
        3. user's stored preference (M13)         -> honour the explicit choice
        4. DEFAULT_LANG ("en")                    -> last resort

    ``preference`` is a stub for now; M13 will supply the stored value.

    Args:
        text (str): The input text to detect the language of.
        previous_lang (str | None, optional): The language of the previous turn. Defaults to None.
        preference (str | None, optional): The user's stored language preference. Defaults to None.
        min_confidence (float, optional): The minimum confidence level for detection. Defaults to MIN_CONFIDENCE.

    Returns:
        str: The resolved language tag for this turn.
    """
    detected, confidence = detect_lang(text)
    if detected is not None and confidence >= min_confidence:
        return detected
    if previous_lang:
        return previous_lang
    if preference:
        return preference
    return DEFAULT_LANG
