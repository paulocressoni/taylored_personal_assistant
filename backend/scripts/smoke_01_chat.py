"""smoke_01_chat.py — does a basic DeepSeek call work?

Empirical goal: print the REAL usage_metadata shape. Don't assume key names.
"""

import pytest

from app.core.llm import get_chat_model

pytestmark = pytest.mark.integration

PROMPT = "Reply with exactly one word: pong."


def _run():
    return get_chat_model("responder").invoke(PROMPT)


def test_smoke_01_chat():
    reply = _run()
    assert reply.content.strip()
    print("content       :", reply.content)
    print("usage_metadata:", reply.usage_metadata)


if __name__ == "__main__":
    reply = _run()
    print("content       :", reply.content)
    print("usage_metadata:", reply.usage_metadata)
