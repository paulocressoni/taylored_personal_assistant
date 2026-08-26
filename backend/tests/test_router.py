"""Unit tests for the router — tag parsing (pure) + node-level fallback.

Two levels, mirroring M06's tiers at unit scale:
  Level 1 — parse_route(): a PURE function. No LLM, no mocking, instant.
  Level 2 — router_node(): the real node with get_chat_model swapped for a
            FakeChatModel. Proves prompt-build -> invoke -> parse wiring with
            zero API calls.

Importing from conftest is safe: pytest auto-loads conftest.py and, because
tests/ has no __init__.py, puts this directory on sys.path (prepend mode).
"""

import pytest
from conftest import FakeChatModel, make_state
from langchain_core.messages import AIMessage

from app.graph.nodes.router import DEFAULT_ROUTE, VALID_ROUTES, parse_route, router_node

# --- Level 1: parse_route (pure logic) ----------------------------------


@pytest.mark.parametrize("route", sorted(VALID_ROUTES))
def test_parse_route_accepts_every_valid_route(route: str) -> None:
    assert parse_route(f"<route>{route}</route>") == route


def test_parse_route_is_case_insensitive() -> None:
    assert parse_route("<route>KNOWLEDGE</route>") == "knowledge"


def test_parse_route_ignores_surrounding_text() -> None:
    assert parse_route("Sure! <route>time</route> here you go.") == "time"


# --- the no-tag-found fallback (the case M06 calls out) -----------------


def test_parse_route_no_tag_falls_back_to_default() -> None:
    assert parse_route("I'll just answer directly.") == DEFAULT_ROUTE


def test_parse_route_empty_tag_falls_back() -> None:
    assert parse_route("<route></route>") == DEFAULT_ROUTE


def test_parse_route_unknown_tag_falls_back() -> None:
    assert parse_route("<route>banana</route>") == DEFAULT_ROUTE


def test_parse_route_empty_string_falls_back() -> None:
    assert parse_route("") == DEFAULT_ROUTE


# --- Level 2: router_node with a faked model (dependency injection) -----


def test_router_node_extracts_route_from_fake_reply(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="<route>weather</route>"))
    patch_llm(lambda role: fake)

    result = router_node(make_state(user_input="Is it sunny tomorrow?"), config=None)

    assert result["route"] == "weather"
    assert result["llm_calls"] == 1  # llm_call now reports the call count
    assert len(fake.calls) == 1  # exactly one model call was made


def test_router_node_no_tag_falls_back_to_default(patch_llm) -> None:
    fake = FakeChatModel(AIMessage(content="I don't know, let me think..."))
    patch_llm(lambda role: fake)

    result = router_node(make_state(user_input="hi"), config=None)

    assert result["route"] == DEFAULT_ROUTE
    assert result["llm_calls"] == 1  # llm_call now reports the call count
