"""Unit tests for BE-08 — detect_lang_node reads the persisted previous lang.

Covers:
  - _previous_user_lang reads state["lang"] (no history walk, no re-detect)
  - first turn (no persisted lang) still returns None
  - high-confidence current detection beats the persisted language
  - low-confidence current input falls back to the persisted language
  - two-turn continuation through the real graph + checkpointer keeps the
    language when turn 2 is ambiguous (guards the seeder not resetting lang)
"""

from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.api.deps import build_initial_state
from app.graph.graph import build_graph
from app.graph.nodes.detect_lang import _previous_user_lang, detect_lang_node

# --- _previous_user_lang: persisted state is the source of truth -----------


def test_previous_user_lang_returns_persisted_lang() -> None:
    state = make_state(lang="de")
    assert _previous_user_lang(state) == "de"


def test_previous_user_lang_none_without_prior_turn() -> None:
    state = make_state(lang=None)
    assert _previous_user_lang(state) is None


def test_previous_user_lang_ignores_message_history() -> None:
    # Continuity rides on state["lang"], NOT on walking the message list.
    state = make_state(
        lang=None,
        messages=[HumanMessage(content="Wie ist das Wetter?")],
    )
    assert _previous_user_lang(state) is None


# --- detect_lang_node: fallback semantics ----------------------------------


def _patch_detection(monkeypatch, result) -> None:
    # resolve_lang resolves the module-global detect_lang at call time, so
    # patching app.language.detector.detect_lang is the right seam.
    monkeypatch.setattr("app.language.detector.detect_lang", lambda text: result)


def test_detect_high_confidence_beats_persisted_lang(monkeypatch) -> None:
    _patch_detection(monkeypatch, ("de", 0.95))
    state = make_state(user_input="anything", lang="en")
    assert detect_lang_node(state, config=None) == {"lang": "de"}


def test_detect_low_confidence_falls_back_to_persisted_lang(monkeypatch) -> None:
    _patch_detection(monkeypatch, ("en", 0.2))
    state = make_state(user_input="anything", lang="de")
    assert detect_lang_node(state, config=None) == {"lang": "de"}


# --- end-to-end: two checkpointed turns keep the language ------------------


def test_two_turn_continuation_keeps_persisted_lang(monkeypatch, patch_llm) -> None:
    # Script all four model calls (router + responder per turn) on one fake.
    fake = FakeChatModel(
        AIMessage(content="<route>responder</route>"),
        AIMessage(content="Antwort auf Deutsch."),
        AIMessage(content="<route>responder</route>"),
        AIMessage(content="Antwort bleibt Deutsch."),
    )
    patch_llm(lambda role: fake)

    # Turn 1 detects German confidently; turn 2 is ambiguous/low-confidence.
    detections = iter([("de", 0.95), ("en", 0.2)])
    monkeypatch.setattr(
        "app.language.detector.detect_lang", lambda text: next(detections)
    )

    checkpointer = InMemorySaver()
    graph = build_graph(checkpointer=checkpointer)
    config = {"configurable": {"thread_id": "session-1"}}

    final1 = graph.invoke(
        build_initial_state(
            user_input="Wie ist das Wetter?",
            session_id="session-1",
            channel="api",
            device_id=None,
        ),
        config=config,
    )
    assert final1["lang"] == "de"
    assert len(final1["messages"]) == 2  # Human + AI

    final2 = graph.invoke(
        build_initial_state(
            user_input="ok",
            session_id="session-1",
            channel="api",
            device_id=None,
        ),
        config=config,
    )
    # Low-confidence turn 2 must NOT reset to the raw detection: it falls
    # back to the persisted "de" from turn 1.
    assert final2["lang"] == "de"
    assert len(final2["messages"]) == 4  # history kept growing
