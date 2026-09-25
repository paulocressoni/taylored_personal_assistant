"""Unit tests for RunTelemetry's token and error aggregation."""

from app.core.callbacks import RunTelemetry, _classify_llm_error


def test_add_usage_reads_cache_hit_from_nested_input_token_details() -> None:
    telemetry = RunTelemetry()
    telemetry._add_usage(
        {
            "input_tokens": 473,
            "output_tokens": 41,
            "total_tokens": 514,
            "input_token_details": {"cache_read": 256},
            "output_token_details": {},
        }
    )
    assert telemetry.cache_hit_tokens == 256
    assert telemetry.cache_miss_tokens == 217
    assert telemetry.totals()["cache_hit_ratio"] == round(256 / 473, 4)


def test_add_usage_reads_flat_vendor_cache_keys() -> None:
    telemetry = RunTelemetry()
    telemetry._add_usage(
        {
            "input_tokens": 300,
            "output_tokens": 10,
            "prompt_cache_hit_tokens": 100,
            "prompt_cache_miss_tokens": 200,
        }
    )
    assert telemetry.cache_hit_tokens == 100
    assert telemetry.cache_miss_tokens == 200


def test_add_usage_without_cache_reporting_counts_everything_as_miss() -> None:
    telemetry = RunTelemetry()
    telemetry._add_usage({"input_tokens": 120, "output_tokens": 5})
    assert telemetry.cache_hit_tokens == 0
    assert telemetry.cache_miss_tokens == 120


def test_classify_llm_error_buckets_rate_limit_and_falls_back_to_class_name() -> None:
    assert _classify_llm_error(RuntimeError("429 Too Many Requests")) == "rate_limit"
    assert _classify_llm_error(TimeoutError("request timed out")) == "timeout"
    assert _classify_llm_error(ValueError("bad json")) == "ValueError"
