"""Unit tests for the pure trace-attribute builder (no Langfuse needed)."""

from app.core.config import settings
from app.core.observability import (
    MAX_METADATA_VALUE_LEN,
    TRACE_SCHEMA_VERSION,
    _error_taxonomy,
    trace_attributes,
)


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
