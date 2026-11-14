"""smoke_06_multilingual_routing.py — multilingual assistant proof.

The single-assistant graph has no router: tool calls are the routing mechanism.
This smoke run covers the M05 intent that survives the collapse:
  - the reply is in the detected language;
  - a plain question reports mode=chat and no tool;
  - a calculation reports mode=tools and the calculate tool;
  - weather/time requests are answered honestly as unavailable (no tool exists
    for them yet), instead of pretending an action happened.

The reply's language is verified with the SAME deterministic detector used in
production (dogfooding) — no second LLM call, no guesswork.
"""

import asyncio
import uuid

import pytest

from app.api.deps import build_initial_state
from app.core.callbacks import RunTelemetry
from app.graph.graph import build_graph
from app.graph.utils import get_last_message
from app.language.detector import detect_lang

pytestmark = pytest.mark.integration


class Case:
    """One smoke utterance and its expected outcome."""

    __slots__ = ("honesty", "lang", "mode", "text", "tool")

    def __init__(
        self,
        text: str,
        mode: str,
        lang: str,
        tool: str | None = None,
        honesty: tuple[str, ...] = (),
    ) -> None:
        self.text = text
        self.mode = mode
        self.lang = lang
        self.tool = tool
        self.honesty = honesty


CASES = [
    Case("Was ist die Hauptstadt von Frankreich?", "chat", "de"),
    Case("Was ist 15% von 240?", "tools", "de", tool="calculate"),
    Case("Wie ist das Wetter?", "chat", "de", honesty=("nicht", "leider", "noch")),
    Case("Que horas são?", "chat", "pt-BR", honesty=("não", "infelizmente", "ainda")),
]


async def _invoke(user_input: str) -> dict:
    initial = build_initial_state(
        user_input=user_input,
        session_id=f"smoke-06-{uuid.uuid4().hex}",
        channel="cli",
        device_id=None,
    )
    telemetry = RunTelemetry()
    graph = build_graph()
    return await graph.ainvoke(
        initial,
        config={
            "callbacks": [telemetry],
            "configurable": {"run_telemetry": telemetry},
        },
    )


async def test_smoke_06_multilingual_assistant() -> None:
    for case in CASES:
        final = await _invoke(case.text)
        reply = str(get_last_message(final["messages"]).content)

        assert final["lang"] == case.lang, (
            f"input={case.text!r}: state lang = {final['lang']!r}, want {case.lang!r}"
        )
        assert final["mode"] == case.mode, (
            f"input={case.text!r}: mode = {final['mode']!r}, want {case.mode!r}"
        )

        tools_called = final.get("tools_called") or []
        if case.tool is None:
            assert not tools_called, (
                f"input={case.text!r}: unexpected tools {tools_called!r}"
            )
        else:
            assert case.tool in tools_called, (
                f"input={case.text!r}: tools = {tools_called!r}, want {case.tool!r}"
            )

        reply_lang, confidence = detect_lang(reply)
        assert reply_lang == case.lang, (
            f"input={case.text!r}: reply language = {reply_lang!r} "
            f"(conf={confidence:.2f}), want {case.lang!r}; reply={reply!r}"
        )

        if case.honesty:
            lowered = reply.lower()
            assert any(word in lowered for word in case.honesty), (
                f"input={case.text!r}: reply does not admit the action is "
                f"unavailable; reply={reply!r}"
            )

        print(
            f"[{case.lang}] input={case.text!r} -> "
            f"mode={final['mode']!r} lang={final['lang']!r} "
            f"tools={tools_called!r} reply_lang={reply_lang!r} "
            f"conf={confidence:.2f}"
        )
        print(f"   reply: {reply}")


async def _main() -> None:
    for case in CASES:
        final = await _invoke(case.text)
        reply = str(get_last_message(final["messages"]).content)
        print(f"input={case.text!r}")
        print(
            f"  mode={final['mode']!r} lang={final['lang']!r} "
            f"tools={final.get('tools_called')!r}"
        )
        print(f"  reply={reply!r}")


if __name__ == "__main__":
    asyncio.run(_main())
