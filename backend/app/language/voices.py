"""Voice selection per app language for text-to-speech.

WHY A TABLE AND NOT A PROMPT: the language is already resolved deterministically
by ``app.language.detector``, so choosing a voice is a lookup rather than a
judgement call. A single table also keeps the provider's voice names in one edit
site, which is all a provider swap needs to touch.

Leaf module like its sibling: imports only ``detector``, so nothing here drags in
settings or a provider client.
"""

from collections.abc import Iterable

from app.language.detector import DEFAULT_LANG, SUPPORTED_LANGS

# One voice per supported language. The values are `gpt-4o-mini-tts` voice names,
# chosen so each language gets a distinct timbre rather than one shared persona.
VOICE_BY_LANG: dict[str, str] = {
    "en": "marin",
    "de": "cedar",
    "pt-BR": "coral",
}

# `pt` and `pt-BR` are the same language to a voice list, and speech-to-text
# reports the bare tag, so region-less input resolves through the base subtag.
# Assumes one variant per base language: a second `pt-XX` key would silently win.
_VOICE_BY_BASE_LANG: dict[str, str] = {
    tag.split("-")[0].lower(): voice for tag, voice in VOICE_BY_LANG.items()
}


def _check_coverage(langs: Iterable[str]) -> None:
    """Fail fast when a language that can be detected has no voice.

    Args:
        langs: Language tags that must be present in `VOICE_BY_LANG`.

    Raises:
        RuntimeError: if any tag has no voice.
    """
    missing = sorted(tag for tag in langs if tag not in VOICE_BY_LANG)
    if missing:
        raise RuntimeError(
            f"VOICE_BY_LANG has no voice for {', '.join(missing)}; every "
            "language in SUPPORTED_LANGS needs one."
        )


# Import-time guard: a language the assistant can detect but not speak would
# answer in the wrong voice, which is worse than refusing to start.
_check_coverage(SUPPORTED_LANGS)

DEFAULT_VOICE = VOICE_BY_LANG[DEFAULT_LANG]


def voice_for(lang: str | None) -> str:
    """Return the voice to speak `lang` with.

    Unknown, missing and region-less tags degrade to the default language's voice
    rather than raising: a voice turn must always be answerable, and the tag comes
    from detection, a stored preference or an env override, none of which this
    module controls.

    Args:
        lang: An app language tag such as `en`, `de`, `pt-BR` or `pt`, or `None`
            when the turn has no resolved language yet.

    Returns:
        A provider voice name.
    """
    if not lang:
        return DEFAULT_VOICE
    if lang in VOICE_BY_LANG:
        return VOICE_BY_LANG[lang]
    return _VOICE_BY_BASE_LANG.get(lang.split("-")[0].strip().lower(), DEFAULT_VOICE)
