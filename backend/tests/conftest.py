"""Shared fixtures for unit tests — faking the LLM and the voice providers,
no network and no key.

conftest.py is auto-loaded by pytest, so everything here is available to
every test file in this directory without an explicit import.

The star of the show is FakeChatModel: the M06 pattern of replacing the real
(paid, non-deterministic, network-bound) chat model with a scripted fake.
"""

from collections.abc import AsyncIterator, Callable
from typing import Self

import pytest
from langchain_core.messages import BaseMessage

from app.voice.stt import Transcript


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
        "tool_outcomes": [],
        "permission_denials": [],
        "usage": None,
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


# ---------------------------------------------------------------------------
# Voice doubles. Same idea as FakeChatModel: no provider is ever contacted, and
# every scripted stream runs out loudly rather than silently repeating.
# ---------------------------------------------------------------------------


def transcript(text: str, language_hint: str | None = None) -> Transcript:
    """Build the successful `Transcript` a fake transcriber should return.

    Args:
        text: What the provider is pretending to have heard.
        language_hint: The language tag the session sent, or None.

    Returns:
        A successful `Transcript` whose provider marks it as fake.
    """
    return Transcript(
        text=text,
        language_hint=language_hint,
        duration_s=1.0,
        provider="fake",
    )


class FakeTranscriber:
    """Duck-typed `Transcriber` replaying scripted results, one per call.

    Distinct from `FakeTranscriptions` in test_voice_stt.py: that double fakes the
    SDK's `transcriptions` resource to test the provider adapter, while this one
    fakes the adapter so a session test never reaches the SDK at all.

    Running out of scripted results raises, so a test that sends one utterance
    more than it scripted fails loudly instead of reusing a stale transcript.
    """

    def __init__(self, *results: Transcript | Exception) -> None:
        self._results = list(results)
        self.calls: list[tuple[bytes, str | None]] = []

    async def transcribe(
        self, pcm: bytes, language_hint: str | None = None
    ) -> Transcript:
        """Record the call, then return or raise the next scripted result."""
        self.calls.append((pcm, language_hint))
        if not self._results:
            raise AssertionError("FakeTranscriber ran out of scripted results")
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeSynthesizer:
    """Duck-typed `Synthesizer` streaming the same scripted PCM for every call.

    The payload is fixed rather than a queue because a provider's chunk
    boundaries are arbitrary: only the concatenation is meaningful PCM. Tests
    that need to know how many sentences were spoken read `requests`.

    `raise_on` fails the Nth call (1-based) from INSIDE the generator, which is
    where a real provider fails — on the first `__anext__`, not at call time, so
    a session that guards only the call still has to handle it.
    """

    def __init__(self, *chunks: bytes, raise_on: int | None = None) -> None:
        self._chunks = chunks
        self.raise_on = raise_on
        self.requests: list[tuple[str, str]] = []

    async def synthesize(self, text: str, voice: str) -> AsyncIterator[bytes]:
        """Record the request and stream the scripted chunks."""
        self.requests.append((text, voice))
        if self.raise_on == len(self.requests):
            raise RuntimeError("FakeSynthesizer was told to fail on this call")
        for chunk in self._chunks:
            yield chunk


class ScriptedVad:
    """Duck-typed `Vad` returning scripted probabilities, one per window.

    The script models the AUDIO, not the model: `reset()` records the call
    without clearing the queue. That is what lets one test drive the real
    `EndpointDetector` through onset, hysteresis and recovery in a single stream.
    """

    def __init__(self, *probabilities: float) -> None:
        self._probabilities = list(probabilities)
        self.windows: list[bytes] = []
        self.resets = 0

    def feed(self, *probabilities: float) -> Self:
        """Append to the script, returning self so calls chain."""
        self._probabilities.extend(probabilities)
        return self

    def reset(self) -> None:
        """Record that the detector reset, without rewinding the audio script."""
        self.resets += 1

    def speech_probability(self, window: bytes) -> float:
        """Record the window and return the next scripted probability.

        Raises:
            AssertionError: if the script has been consumed.
        """
        self.windows.append(window)
        if not self._probabilities:
            raise AssertionError("ScriptedVad ran out of scripted probabilities")
        return self._probabilities.pop(0)


@pytest.fixture
def fake_vad() -> ScriptedVad:
    """A fresh scripted VAD per test.

    Function-scoped on purpose: the script is consumed as it is read, so a
    module- or session-scoped instance would leak one test's audio — and its
    reset count — into the next.
    """
    return ScriptedVad()
