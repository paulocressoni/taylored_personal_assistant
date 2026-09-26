"""CLI entry point that prints the resolved config for the current environment.

Usage:

    ENV=dev  uv run python -m app.core.cli
    ENV=prod uv run python -m app.core.cli

The two runs must print different resolved configs (see M00 DONE WHEN).
"""

from app.core.config import settings


def main() -> None:
    print(f"env                : {settings.env}")
    print(f"default_timezone   : {settings.default_timezone}")
    print(f"supported_languages: {', '.join(settings.supported_languages)}")
    print(f"deepseek_api_key   : {'<set>' if settings.has_deepseek else '<MISSING>'}")
    print(f"assistant_api_key  : {'<set>' if settings.has_assistant else '<MISSING>'}")
    print(f"voice_enabled      : {settings.voice_enabled}")
    print(f"voice_ready        : {settings.voice_ready}")
    print(f"voice_stt_api_key  : {'<set>' if settings.has_voice_stt else '<MISSING>'}")
    print(f"voice_tts_api_key  : {'<set>' if settings.has_voice_tts else '<MISSING>'}")


if __name__ == "__main__":
    main()
