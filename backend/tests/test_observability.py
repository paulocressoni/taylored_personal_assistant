"""Unit tests for the pure trace-attribute builder and trace-IO helpers."""

from contextlib import contextmanager
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import SecretStr

from app.core import observability
from app.core.config import settings
from app.core.observability import (
    MAX_METADATA_VALUE_LEN,
    MAX_TRACE_IO_CHARS,
    TRACE_SCHEMA_VERSION,
    TURN_TRACE_NAME,
    _error_taxonomy,
    enrich_trace,
    langfuse_metadata,
    mark_turn_cancelled,
    mark_turn_failed,
    stamp_voice_timing,
    timing_metadata,
    trace_attributes,
    turn_io,
)


def test_langfuse_metadata_carries_static_trace_identity() -> None:
    metadata = langfuse_metadata(session_id="s-1", channel="api")

    assert metadata["langfuse_session_id"] == "s-1"
    assert metadata["langfuse_trace_name"] == TURN_TRACE_NAME
    assert metadata["langfuse_tags"] == [f"env:{settings.env}", "channel:api"]
    assert metadata["channel"] == "api"
    assert "langfuse_user_id" not in metadata


def test_langfuse_metadata_adds_user_id_only_when_given() -> None:
    without = langfuse_metadata(session_id="s-1", channel="cli")
    with_user = langfuse_metadata(session_id="s-1", channel="cli", user_id="u-9")

    assert "langfuse_user_id" not in without
    assert with_user["langfuse_user_id"] == "u-9"
    assert with_user["langfuse_tags"] == [f"env:{settings.env}", "channel:cli"]


def test_error_taxonomy_ignores_ok_and_denied_calls() -> None:
    outcomes = [
        {"tool": "calculate", "ok": True, "denied": False, "error_type": None},
        {"tool": "x", "ok": False, "denied": True, "error_type": "PermissionError"},
        {"tool": "x", "ok": False, "denied": False, "error_type": "ValueError"},
        {"tool": "x", "ok": False, "denied": False, "error_type": None},
    ]
    assert _error_taxonomy(outcomes) == {"ValueError": 1, "unknown": 1}


def test_trace_attributes_marks_tool_ok_when_tools_ran() -> None:
    attributes = trace_attributes(
        {
            "route": "knowledge",
            "lang": "en",
            "channel": "api",
            "tool_outcomes": [
                {"tool": "calculate", "ok": True, "denied": False, "error_type": None}
            ],
        }
    )
    assert attributes.name == "assistant:knowledge"
    assert attributes.tags == [f"env:{settings.env}", "channel:api", "status:tool_ok"]
    assert attributes.metadata["schema_version"] == TRACE_SCHEMA_VERSION


def test_trace_attributes_reports_failures_and_denials() -> None:
    attributes = trace_attributes(
        {
            "route": "responder",
            "channel": "api",
            "tool_outcomes": [
                {"tool": "a", "ok": False, "denied": False, "error_type": "ValueError"}
            ],
            "permission_denials": [{"tool": "b", "reason": "PermissionError"}],
            "usage": {"error_types": {"rate_limit": 1}},
        }
    )
    assert attributes.tags == [
        f"env:{settings.env}",
        "channel:api",
        "status:llm_error",
        "status:tool_error",
        "status:permission_denied",
    ]
    assert attributes.metadata["tool_error_taxonomy"] == "ValueError:1"
    assert attributes.metadata["llm_error_taxonomy"] == "rate_limit:1"


def test_trace_attributes_values_fit_the_sdk_limit() -> None:
    attributes = trace_attributes(
        {
            "route": "knowledge",
            "channel": "api",
            "lang": "pt-BR",
            "tools_called": ["calculate", "calculate", "calculate"],
            "tool_outcomes": [
                {"tool": "calculate", "ok": True, "denied": False, "error_type": None}
            ]
            * 3,
            "usage": {
                "llm_calls": 4,
                "input_tokens": 12345,
                "output_tokens": 678,
                "cache_hit_tokens": 9000,
                "cache_miss_tokens": 3345,
                "cache_hit_ratio": 0.729,
                "error_types": {},
            },
        }
    )
    assert all(
        len(value) <= MAX_METADATA_VALUE_LEN for value in attributes.metadata.values()
    )


def test_trace_attributes_summarises_per_call_tool_outcomes() -> None:
    attributes = trace_attributes(
        {
            "route": "knowledge",
            "channel": "api",
            "tool_outcomes": [
                {"tool": "calculate", "ok": True, "denied": False, "duration_ms": 2.4},
                {"tool": "x", "ok": False, "denied": True, "duration_ms": 0.1},
                {
                    "tool": "y",
                    "ok": False,
                    "denied": False,
                    "error_type": "ValueError",
                    "duration_ms": 1.0,
                },
            ],
        }
    )
    assert (
        attributes.metadata["tool_outcomes"]
        == "calculate:ok:2.4,x:denied:0.1,y:ValueError:1.0"
    )


def test_trace_attributes_truncates_a_long_tool_outcome_summary() -> None:
    outcomes = [
        {"tool": "calculate", "ok": True, "denied": False, "duration_ms": 1.0}
    ] * 20
    attributes = trace_attributes(
        {"route": "knowledge", "channel": "api", "tool_outcomes": outcomes}
    )
    summary = attributes.metadata["tool_outcomes"]
    assert len(summary) == MAX_METADATA_VALUE_LEN
    assert summary.startswith("calculate:ok:1.0,calculate:ok:1.0")


def test_timing_metadata_flattens_milliseconds_to_one_decimal_strings() -> None:
    assert timing_metadata({"stt_ms": 200.04, "first_audio_ms": 930.0}) == {
        "stt_ms": "200.0",
        "first_audio_ms": "930.0",
    }


def test_trace_attributes_records_the_transcription_bias() -> None:
    attributes = trace_attributes(
        {"route": "responder", "lang": "de", "stt_lang": "en"}
    )

    assert attributes.metadata["stt_lang"] == "en"


def test_trace_attributes_leaves_the_bias_empty_on_a_text_turn() -> None:
    attributes = trace_attributes(
        {"route": "responder", "lang": "en", "stt_lang": None}
    )

    assert attributes.metadata["stt_lang"] == ""


class _FakeSpan:
    """Records the attributes the SDK would write onto the app-root span."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}
        self.updates: list[dict[str, Any]] = []


class _FakeClient:
    """Captures every `update_current_span` call on the shared fake span."""

    def __init__(self, span: _FakeSpan) -> None:
        self._span = span

    def update_current_span(self, **attributes: Any) -> None:
        self._span.updates.append(attributes)


def _recording_propagate(span: _FakeSpan) -> Any:
    """Build a `propagate_attributes` stand-in writing onto `span`.

    Metadata lands as per-key span attributes (the SDK behaviour the ordering
    test depends on); tags and trace name are recorded whole.
    """

    @contextmanager
    def _record(**attributes: Any):
        for key, value in (attributes.get("metadata") or {}).items():
            span.attributes[f"langfuse.trace.metadata.{key}"] = str(value)
        if attributes.get("tags") is not None:
            span.attributes["langfuse.trace.tags"] = list(attributes["tags"])
        if attributes.get("trace_name") is not None:
            span.attributes["langfuse.trace.name"] = attributes["trace_name"]
        yield

    return _record


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _FakeSpan:
    """Point the observability helpers at a recording fake with Langfuse on."""
    span = _FakeSpan()
    monkeypatch.setattr(
        observability, "propagate_attributes", _recording_propagate(span)
    )
    monkeypatch.setattr(observability, "get_client", lambda: _FakeClient(span))
    monkeypatch.setattr(observability.settings, "langfuse_enabled", True)
    monkeypatch.setattr(
        observability.settings, "langfuse_public_key", SecretStr("pk-lf-test")
    )
    monkeypatch.setattr(
        observability.settings, "langfuse_secret_key", SecretStr("sk-lf-test")
    )
    return span


def test_turn_io_picks_the_last_human_message() -> None:
    question, reply = turn_io(
        {
            "messages": [
                HumanMessage(content="first question"),
                AIMessage(content="first answer"),
                HumanMessage(content="second question"),
                AIMessage(content="second answer"),
            ]
        }
    )
    assert question == "second question"
    assert reply == "second answer"


def test_turn_io_returns_empty_strings_for_an_empty_state() -> None:
    assert turn_io({}) == ("", "")
    assert turn_io({"messages": []}) == ("", "")


def test_turn_io_truncates_a_long_reply_to_the_cap() -> None:
    question, reply = turn_io(
        {
            "messages": [
                HumanMessage(content="hi"),
                AIMessage(content="x" * (MAX_TRACE_IO_CHARS + 500)),
            ]
        }
    )
    assert question == "hi"
    assert len(reply) == MAX_TRACE_IO_CHARS


def test_turn_io_flattens_block_content() -> None:
    state = {
        "messages": [
            HumanMessage(
                content=[
                    {"type": "text", "text": "hello "},
                    {"type": "text", "text": "world"},
                ]
            ),
            AIMessage(content="hi there"),
        ]
    }
    assert turn_io(state) == ("hello world", "hi there")


def test_enrich_then_stamp_voice_timing_keeps_both_metadata_writes(
    recorder: _FakeSpan,
) -> None:
    enrich_trace({"route": "knowledge", "channel": "voice", "messages": []})
    stamp_voice_timing({"stt_ms": 200.0})

    assert (
        recorder.attributes["langfuse.trace.metadata.schema_version"]
        == TRACE_SCHEMA_VERSION
    )
    assert recorder.attributes["langfuse.trace.metadata.route"] == "knowledge"
    assert recorder.attributes["langfuse.trace.metadata.stt_ms"] == "200.0"


def test_mark_turn_failed_keeps_the_base_tags_and_sets_error_level(
    recorder: _FakeSpan,
) -> None:
    mark_turn_failed("TimeoutError: graph run timed out", channel="api")

    assert recorder.attributes["langfuse.trace.tags"] == [
        f"env:{settings.env}",
        "channel:api",
        "status:failed",
    ]
    assert recorder.updates == [
        {"level": "ERROR", "status_message": "TimeoutError: graph run timed out"}
    ]


def test_mark_turn_cancelled_stamps_a_barge_in(recorder: _FakeSpan) -> None:
    mark_turn_cancelled("barge-in", channel="voice")

    assert recorder.updates == [{"level": "WARNING", "status_message": "barge-in"}]
    assert "status:cancelled" in recorder.attributes["langfuse.trace.tags"]
