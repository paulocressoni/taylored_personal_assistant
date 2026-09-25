"""Per-run telemetry: count LLM calls and aggregate token usage for one run.

How it works: a single `RunTelemetry` instance is created at the invoke site
and passed to `graph.invoke(..., config={"callbacks": [telemetry]})`.
LangGraph threads those callbacks down through every node, so the handler
fires for ALL model/tool calls in the run — including calls the nodes never
persist to state (e.g. the router's classification call). The same instance is
also exposed to nodes via `config["configurable"]["run_telemetry"]` so the
telemetry node can write the counts into IPAState at the end of the run.

Recent additions:
* Prompt-cache tokens (hit + miss) are captured from each response's
  `usage_metadata` using a set of known field names, so the cache-hit-ratio
  dashboard has data without a model-specific parse.
* Failed LLM calls are bucketed into a small taxonomy (rate_limit / auth /
  timeout / exception-name) on `on_llm_error` instead of vanishing silently.
* `totals()` returns everything telemetry_node needs in one dict.
"""

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import (
    ChatGeneration,
    ChatGenerationChunk,
    LLMResult,
)

logger = logging.getLogger(__name__)

# Prompt-cache token field names. DeepSeek/openai-compatible models each name
# these slightly differently, so we scan a conservative set and take the first
# present value rather than hard-coding one vendor's schema.
_CACHE_HIT_KEYS = (
    "cache_read",
    "prompt_cache_hit_tokens",
    "cache_read_input_tokens",
    "cached_tokens",
    "input_tokens_cached",
    "cache_hit_tokens",
)
_CACHE_MISS_KEYS = (
    "cache_creation",
    "prompt_cache_miss_tokens",
    "cache_creation_input_tokens",
    "non_cached_tokens",
    "uncached_tokens",
    "cache_miss_tokens",
)

# Marker substrings (lower-cased) used to bucket an LLM error into a small
# taxonomy. Anything unrecognised falls back to its exception class name so no
# failure is ever lost from the dashboards.
_RATE_LIMIT_MARKERS = (
    "ratelimit",
    "rate limit",
    "429",
    "quota",
    "throttl",
    "too many requests",
)
_AUTH_MARKERS = ("auth", "401", "403", "api key", "apikey", "unauthorized", "forbidden")
_TIMEOUT_MARKERS = ("timeout", "timed out", "deadline exceeded")


def _first_key(usage: dict[str, Any], keys: tuple[str, ...]) -> int:
    """Return the value of the first present cache-token key, else 0.

    Args:
        usage: A model `usage_metadata` dict (keys vary by vendor).
        keys: Candidate field names to scan, in priority order.

    Returns:
        The integer token count found, or 0 when no candidate key exists.
    """
    for key in keys:
        value = usage.get(key)
        if value is not None:
            return int(value)
    return 0


def _classify_llm_error(error: BaseException) -> str:
    """Bucket an LLM error into the trace error taxonomy.

    Args:
        error: The exception the chat model raised.

    Returns:
        One of `rate_limit`, `auth`, `timeout`, or the exception class name.
    """
    name = type(error).__name__
    signal = f"{name} {error}".lower()
    if any(marker in signal for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if any(marker in signal for marker in _AUTH_MARKERS):
        return "auth"
    if any(marker in signal for marker in _TIMEOUT_MARKERS):
        return "timeout"
    return name


class RunTelemetry(BaseCallbackHandler):
    """Count LLM invocations and aggregate token usage for a single graph run.

    One instance per `graph.invoke(...)` so counters reset every turn —
    never reuse across sessions or you'll count the previous turn's work.
    Counters are plain ints; token totals are accumulated from each
    response's `usage_metadata` so they reflect real API usage. Cache-token
    and error-bucket counters feed the production cost and reliability
    dashboards.
    """

    def __init__(self) -> None:
        self.llm_calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cache_hit_tokens: int = 0
        self.cache_miss_tokens: int = 0
        # taxonomy bucket name -> number of failed LLM calls in this run.
        self.error_types: dict[str, int] = {}

    def _add_usage(self, usage: dict[str, Any] | None) -> None:
        """Accumulate one response's token usage, including prompt-cache fields.

        Args:
            usage: A generation's `usage_metadata` dict, or None when absent.
        """
        usage = usage or {}
        input_tokens = int(usage.get("input_tokens", 0))
        self.input_tokens += input_tokens
        self.output_tokens += int(usage.get("output_tokens", 0))
        # Cache counts arrive NESTED on most providers — langchain normalises
        # them into `input_token_details`, raw openai-style responses use
        # `prompt_tokens_details` — and FLAT on a few. Scanning only the top
        # level silently booked every cached token as a miss.
        hit = 0
        miss = 0
        for detail_key in ("input_token_details", "prompt_tokens_details"):
            detail = usage.get(detail_key)
            if isinstance(detail, dict):
                hit = hit or _first_key(detail, _CACHE_HIT_KEYS)
                miss = miss or _first_key(detail, _CACHE_MISS_KEYS)
        hit = hit or _first_key(usage, _CACHE_HIT_KEYS)
        miss = miss or _first_key(usage, _CACHE_MISS_KEYS)
        if hit and not miss:
            # Vendor reports cache reads but no explicit miss field: the rest
            # of the prompt was served uncached, so derive the miss portion.
            miss = max(0, input_tokens - hit)
        elif not hit and not miss:
            # No cache reporting at all — every input token is a cache miss.
            miss = input_tokens
        self.cache_hit_tokens += hit
        self.cache_miss_tokens += miss

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
        # are what you'd feed into a cost estimate later.
        for generations in response.generations:
            for generation in generations:
                # Only chat generations carry .message; the isinstance narrows
                # the union so mypy knows .message exists (it always will for
                # ChatDeepSeek, but non-chat generations are handled safely).
                if isinstance(generation, (ChatGeneration, ChatGenerationChunk)):
                    usage = getattr(generation.message, "usage_metadata", None)
                    self._add_usage(usage)

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        # Bucket failures so the error-taxonomy dashboard can show WHY calls
        # failed (rate limit vs auth vs timeout vs model-specific), not just
        # that they did. Never log the raw error text — it can embed secrets.
        bucket = _classify_llm_error(error)
        self.error_types[bucket] = self.error_types.get(bucket, 0) + 1
        logger.warning("llm call failed during run: %s", bucket)

    def totals(self) -> dict[str, Any]:
        """Authoritative per-run usage, ready for `telemetry_node` to persist.

        Returns:
            Dict with `llm_calls`, `input_tokens`, `output_tokens`,
            `cache_hit_tokens`, `cache_miss_tokens`, `cache_hit_ratio`
            (rounded to 4dp, or None when no tokens flowed) and `error_types`.
        """
        hit, miss = self.cache_hit_tokens, self.cache_miss_tokens
        ratio = round(hit / (hit + miss), 4) if hit + miss else None
        return {
            "llm_calls": self.llm_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_hit_tokens": hit,
            "cache_miss_tokens": miss,
            "cache_hit_ratio": ratio,
            "error_types": dict(self.error_types),
        }
