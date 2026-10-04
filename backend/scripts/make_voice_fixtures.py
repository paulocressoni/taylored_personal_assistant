"""make_voice_fixtures.py — synthesize the three STT fixtures, once.

The smoke scripts and the latency tool need one known-good spoken sentence per
language. Keeping the recipe in the repo (rather than an opaque binary dropped in
by hand) is what makes them reproducible: re-running this file regenerates them.

Writes `tests/fixtures/voice_{en,de,pt}.wav`, mono 16-bit at the pipeline's
INPUT rate — byte-for-byte the audio a browser or the reSpeaker sends.

Usage (from backend/, with the voice keys in .env.dev):
    uv run python scripts/make_voice_fixtures.py
    uv run python scripts/make_voice_fixtures.py de
"""

import asyncio
import sys
from pathlib import Path

from app.core.config import settings
from app.language.voices import voice_for
from app.voice.audio import duration_s, resample_pcm, wrap_wav
from app.voice.registry import build_voice_providers
from app.voice.tts import PCM_SAMPLE_RATE

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"

# What each fixture SAYS. A factual question is deliberate: it exercises the one
# capability the assistant actually has, so the graph produces a real answer
# instead of an apology, and the language detector has something unambiguous to
# work with.
SPOKEN = {
    "en": "What is the capital of France?",
    "de": "Was ist die Hauptstadt von Frankreich?",
    "pt": "Qual é a capital da França?",
}
LANG_TAG = {"en": "en", "de": "de", "pt": "pt-BR"}


async def generate(langs: list[str]) -> list[Path]:
    """Speak each language's sentence and write it as a 16 kHz WAV.

    Args:
        langs: Fixture languages to (re)generate.

    Returns:
        The paths written, in the order requested.

    Raises:
        SystemExit: if voice is not configured, or a language is unknown.
    """
    providers = build_voice_providers()
    if providers is None:
        raise SystemExit(
            "voice is not configured: set VOICE_ENABLED=true and both provider keys"
        )
    unknown = [lang for lang in langs if lang not in SPOKEN]
    if unknown:
        raise SystemExit(f"unknown fixture language(s): {', '.join(unknown)}")

    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for lang in langs:
        text = SPOKEN[lang]
        voice = voice_for(LANG_TAG[lang])
        chunks = [
            chunk async for chunk in providers.synthesizer.synthesize(text, voice)
        ]
        # Convert here, once, so no consumer ever has to: a fixture that can
        # drift in rate is a fixture that silently invalidates a latency run.
        pcm = resample_pcm(
            b"".join(chunks), PCM_SAMPLE_RATE, settings.voice_input_sample_rate
        )
        path = FIXTURE_DIR / f"voice_{lang}.wav"
        path.write_bytes(wrap_wav(pcm, settings.voice_input_sample_rate))
        seconds = duration_s(pcm, settings.voice_input_sample_rate)
        print(f'{path.name}: {seconds:.2f}s ({voice}) — "{text}"')
        written.append(path)
    return written


def main() -> None:
    """Regenerate the requested fixtures, or all of them."""
    langs = sys.argv[1:] or list(SPOKEN)
    paths = asyncio.run(generate(langs))
    print(f"\n{len(paths)} fixture(s) written to {FIXTURE_DIR}")


if __name__ == "__main__":
    main()
