"""Text-to-speech voice selection per app language.

The TABLE lives in ``Settings.voice_tts_voices`` so an operator can change a
voice without a code change, and the config validator already refuses to start
when a supported language has none. This module therefore owns only the
RESOLUTION rules: what an unknown, missing or region-less tag falls back to.

Reads settings, so unlike its leaf sibling ``detector`` it is not a leaf module.
"""

from app.core.config import settings
from app.language.detector import DEFAULT_LANG


def _voices_by_base_lang() -> dict[str, str]:
    """Return the configured voices keyed by primary language subtag.

    `pt` and `pt-BR` are the same language to a voice list, and speech-to-text
    reports the bare tag, so region-less input resolves through the base subtag.
    Assumes one variant per base language: a second `pt-XX` entry would silently
    win.

    Returns:
        Base subtag -> voice, derived from the configured table.
    """
    return {
        tag.split("-")[0].lower(): voice
        for tag, voice in settings.voice_tts_voices.items()
    }


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
    voices = settings.voice_tts_voices
    default = voices[DEFAULT_LANG]
    if not lang:
        return default
    if lang in voices:
        return voices[lang]
    return _voices_by_base_lang().get(lang.split("-")[0].strip().lower(), default)
