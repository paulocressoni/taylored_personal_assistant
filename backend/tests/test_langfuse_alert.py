"""Unit tests for the Langfuse alert probe's response parsing.

`_sum_rows` is the only code that knows the Metrics API v2 response shape, and
it is stdlib-only by design (the probe runs from cron), so nothing else
exercises it. It previously read the row array from `rows` while Langfuse 4.30
returns it under `data` — every run then exited 1 ("probe failure") instead of
reporting a severity, silently disabling the whole alerting path.
"""

from scripts.langfuse_alert import COST_TOKENS, COUNT_TOKENS, _sum_rows

# --- the shape Langfuse 4.30 actually returns ------------------------------


def test_sum_rows_reads_the_data_key() -> None:
    total, field = _sum_rows({"data": [{"sum_totalCost": 0.25}]}, COST_TOKENS)
    assert total == 0.25
    assert field == "sum_totalCost"


def test_sum_rows_sums_across_every_row() -> None:
    payload = {"data": [{"sum_totalCost": 0.1}, {"sum_totalCost": 0.15}]}
    total, field = _sum_rows(payload, COST_TOKENS)
    assert round(total, 4) == 0.25
    assert field == "sum_totalCost"


def test_sum_rows_reads_the_error_count_field() -> None:
    total, field = _sum_rows({"data": [{"count_count": 4}]}, COUNT_TOKENS)
    assert total == 4.0
    assert field == "count_count"


# --- backward compatibility with the older `rows` key ----------------------


def test_sum_rows_still_reads_the_legacy_rows_key() -> None:
    total, field = _sum_rows({"rows": [{"count_count": 4}]}, COUNT_TOKENS)
    assert total == 4.0
    assert field == "count_count"


def test_sum_rows_prefers_data_when_both_keys_are_present() -> None:
    payload = {"data": [{"count_count": 2}], "rows": [{"count_count": 99}]}
    total, _ = _sum_rows(payload, COUNT_TOKENS)
    assert total == 2.0


# --- the "unrecognised shape" path the probe turns into exit code 1 --------


def test_sum_rows_reports_no_match_when_no_token_matches() -> None:
    total, field = _sum_rows({"data": [{"unrelated": 1}]}, COST_TOKENS)
    assert (total, field) == (0.0, None)


def test_sum_rows_reports_no_match_for_missing_or_empty_rows() -> None:
    assert _sum_rows({}, COST_TOKENS) == (0.0, None)
    assert _sum_rows({"data": []}, COST_TOKENS) == (0.0, None)
