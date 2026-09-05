"""Shared fixtures for M06 unit tests — faking the LLM, no network, no key.

conftest.py is auto-loaded by pytest, so everything here is available to
every test file in this directory without an explicit import.

The star of the show is FakeChatModel: the M06 pattern of replacing the real
(paid, non-deterministic, network-bound) chat model with a scripted fake.
"""

from collections.abc import Callable

import pytest
from langchain_core.messages import BaseMessage


class FakeChatModel:
    """Tiny duck-typed chat model for unit tests.

    Why not the real ChatDeepSeek (or even GenericFakeChatModel)?
      - Real model = network + API key + non-determinism. Never in unit tests.
      - langchain-core's GenericFakeChatModel raises NotImplementedError on
        bind_tools() (verified locally), but knowledge_node calls
        get_chat_model("specialist").bind_tools(TOOLS).
      - So we fake exactly the surface the nodes touch:
            .invoke(messages, config=None) -> next scripted response
            .bind_tools(tools)             -> returns self, remembers tools
        and we RECORD every call so a test can assert what the node sent.

    Each .invoke() pops the next scripted response; running out raises loudly
    so a wrong script count fails the test instead of silently passing.
    """

    def __init__(self, *responses: BaseMessage) -> None:
        self._responses = list(responses)
        self.calls: list[list[BaseMessage]] = []
        self.bound_tools: list | None = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    def invoke(self, messages, config=None) -> BaseMessage:
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("FakeChatModel ran out of scripted responses")
        return self._responses.pop(0)


def make_state(**overrides) -> dict:
    """Build a minimal valid IPAState dict with sensible defaults.

    Nodes read keys with both state["..."] and state.get(...); this helper
    stops every test from repeating the full 12-key literal.
    """
    state: dict = {
        "messages": [],
        "user_input": "hello",
        "session_id": "test-session",
        "channel": "cli",
        "device_id": None,
        "principal": None,
        "lang": "en",
        "route": None,
        "pending_action": None,
        "llm_calls": 0,
        "tool_iterations": 0,
        "tools_called": [],
    }
    state.update(overrides)
    return state


@pytest.fixture
def patch_llm(monkeypatch: pytest.MonkeyPatch):
    """Dependency injection: redirect every node's get_chat_model to a fake.

    Nodes do `from app.core.llm import get_chat_model` at MODULE load time, so
    the name lives inside each node module — we must patch it THERE, not in
    app.core.llm. Usage:

        fake = FakeChatModel(AIMessage(content="<route>x</route>"))
        patch_llm(lambda role: fake)
        result = router_node(state, config=None)
    """

    def _patch(factory: Callable[[str], FakeChatModel]) -> None:
        for module in (
            "app.graph.nodes.router",
            "app.graph.nodes.knowledge",
            "app.graph.nodes.responder",
        ):
            monkeypatch.setattr(f"{module}.get_chat_model", factory)

    return _patch
