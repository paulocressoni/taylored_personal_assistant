"""smoke_05_multilingual.py — do en/de/pt-BR replies match the input language
and do umlauts/accented characters survive the round trip?

Uses settings.supported_languages (["en", "de", "pt-BR"]) as the source of truth.
"""

import pytest
from app.core.config import settings
from app.core.llm import get_chat_model

pytestmark = pytest.mark.integration

# (lang, label for the prompt, concept, expected word)
CASES = [
    ("en", "English", "heart", "heart"),
    ("de", "German", "cheese", "Käse"),
    ("pt-BR", "Portuguese", "heart", "coração"),
]


def _run(language: str, label: str, concept: str):
    prompt = f"Reply with exactly one word: the {label} word for '{concept}'."
    return get_chat_model("responder").invoke(prompt)


def test_smoke_05_multilingual():
    # Config is the source of truth — cases must cover exactly what's declared.
    assert [case[0] for case in CASES] == settings.supported_languages
    for language, label, concept, expected in CASES:
        reply = _run(language, label, concept)
        assert expected.lower() in reply.content.lower(), (
            f"lang={language}: expected {expected!r}, got {reply.content!r}"
        )
        print(
            f"[{language}] content={reply.content!r} "
            f"| usage_metadata={reply.usage_metadata}"
        )


if __name__ == "__main__":
    for language, label, concept, _expected in CASES:
        reply = _run(language, label, concept)
        print(f"[{language}] content={reply.content!r}")
        print(f"   usage_metadata={reply.usage_metadata}")
