"""smoke_06_multilingual_routing.py — M05 end-to-end proof.

Does the graph detect the input language, route it correctly, and reply in
the detected language? Covers the M05 DONE WHEN:
  - "Wie ist das Wetter?"  -> route=weather,  reply in German
  - "Que horas são?"       -> route=time,     reply in Portuguese (pt-BR)
  - "Was ist die Hauptstadt von Frankreich?" -> route=knowledge, reply in German

The reply's language is verified with the SAME deterministic detector we use
in production (dogfooding) — no second LLM call, no guesswork.
"""

import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.core.callbacks import RunTelemetry
from app.graph.graph import build_graph
from app.graph.state import IPAState
from app.graph.utils import get_last_message
from app.language.detector import detect_lang

pytestmark = pytest.mark.integration

# (input, expected_route, expected_lang)
CASES = [
    ("Wie ist das Wetter?", "weather", "de"),
    ("Que horas são?", "time", "pt-BR"),
    ("Was ist die Hauptstadt von Frankreich?", "knowledge", "de"),
]


def _invoke(user_input: str) -> dict:
    initial: IPAState = {
        "messages": [HumanMessage(content=user_input)],
        "user_input": user_input,
        "session_id": uuid.uuid4().hex,
        "channel": "cli",
        "device_id": None,
        "principal": None,
        "lang": None,
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
    }
    telemetry = RunTelemetry()
    graph = build_graph()
    return graph.invoke(
        initial,
        config={
            "callbacks": [telemetry],
            "configurable": {"run_telemetry": telemetry},
        },
    )


def test_smoke_06_multilingual_routing() -> None:
    for user_input, expected_route, expected_lang in CASES:
        final = _invoke(user_input)
        reply = str(get_last_message(final["messages"]).content)

        assert final["lang"] == expected_lang, (
            f"input={user_input!r}: state lang = {final['lang']!r}, want {expected_lang!r}"
        )
        assert final["route"] == expected_route, (
            f"input={user_input!r}: route = {final['route']!r}, want {expected_route!r}"
        )
        reply_lang, confidence = detect_lang(reply)
        assert reply_lang == expected_lang, (
            f"input={user_input!r}: reply language = {reply_lang!r} "
            f"(conf={confidence:.2f}), want {expected_lang!r}; reply={reply!r}"
        )

        print(
            f"[{expected_lang}] input={user_input!r} -> "
            f"route={final['route']!r} lang={final['lang']!r} "
            f"reply_lang={reply_lang!r} conf={confidence:.2f}"
        )
        print(f"   reply: {reply}")


if __name__ == "__main__":
    for user_input, expected_route, expected_lang in CASES:
        final = _invoke(user_input)
        reply = str(get_last_message(final["messages"]).content)
        print(f"input={user_input!r}")
        print(f"  route={final['route']!r} lang={final['lang']!r}")
        print(f"  reply={reply!r}")
