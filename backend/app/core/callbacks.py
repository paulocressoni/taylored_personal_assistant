"""Per-run telemetry: count LLM calls and aggregate token usage for one run.

How it works: a single ``RunTelemetry`` instance is created at the invoke site
and passed to ``graph.invoke(..., config={"callbacks": [telemetry]})``.
LangGraph threads those callbacks down through every node, so the handler
fires for ALL model/tool calls in the run — including calls the nodes never
persist to state (e.g. the router's classification call). The same instance is
also exposed to nodes via ``config["configurable"]["run_telemetry"]`` so a
telemetry node can write the counts into IPAState at the end of the run.
"""

from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult


class RunTelemetry(BaseCallbackHandler):
    """Count LLM invocations and aggregate token usage for a single graph run.

    One instance per ``graph.invoke(...)`` so counters reset every turn —
    never reuse across sessions or you'll count the previous turn's work.
    Counters are plain ints; token totals are accumulated from each
    response's ``usage_metadata`` so they reflect real API usage.
    """

    def __init__(self) -> None:
        self.llm_calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # ChatDeepSeek is a CHAT model, so the hook is on_chat_model_start —
        # on_llm_start only fires for non-chat completions and would silently
        # stay at 0. The signature must also be declared explicitly: using
        # *args here makes langchain-core raise an IndexError when it tries to
        # convert `messages` to prompt strings for the fallback path.
        self.llm_calls += 1

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Aggregate real token usage from each generation's usage_metadata.
        # Reasoning tokens are included inside output_tokens, so these totals
        # are what you'd feed into a cost estimate later (see M02 findings).
        for generations in response.generations:
            for generation in generations:
                usage = getattr(generation.message, "usage_metadata", None) or {}
                self.input_tokens += usage.get("input_tokens", 0)
                self.output_tokens += usage.get("output_tokens", 0)
