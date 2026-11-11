"""Tests for the soak report tool.

The fixture under `fixtures/soak/` is deliberately small and hand-checkable: it
carries every shape the tool has to survive — a capture that opens with build
output, ANSI-coloured serial lines, both backend log formats, a 1798 s session,
a lost close frame, a truncated answer, an underrun, and one ASGI traceback.
The numbers below are asserted because they are the ones the documentation
quotes; a silent change to the aggregation must break a test, not a runbook.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.soak_report import (
    ANCHOR_TOLERANCE_S,
    LogError,
    Section,
    build_gate,
    compare,
    derive_anchor,
    percentile,
    report,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "soak"
UNTIL = datetime(2026, 1, 2, tzinfo=UTC)
BOOT = datetime(2026, 1, 1, tzinfo=UTC)


def _rows(section: Section) -> dict[str, str]:
    """Index a rendered section by label, so assertions name the metric."""
    return {label: value for label, value, _denominator in section.rows if label}


def test_percentile_uses_linear_interpolation_not_nearest_rank() -> None:
    # The choice is documented in --help and in docs/soak.md, and it changes the
    # answer on real data: nearest-rank would say 3 here.
    assert percentile([1.0, 3.0, 5.0], 0.5) == 3.0
    # position = (3-1)*0.9 = 1.8, so 5 + (1798-5)*0.8 = 1439.4; the report
    # rounds that to "1439 s" for display.
    assert percentile([3.0, 1798.0, 5.0], 0.9) == pytest.approx(1439.4)


def test_percentile_of_nothing_is_none_not_zero() -> None:
    # A zero would be indistinguishable from a real measurement in a report.
    assert percentile([], 0.5) is None


def test_derive_anchor_recovers_the_device_boot_time() -> None:
    text, payload = report(FIXTURE_DIR, UNTIL)

    meta = payload["meta"]
    assert datetime.fromisoformat(meta["window_start"]) == BOOT
    assert meta["anchor_spread_s"] == pytest.approx(0.0, abs=0.05)
    assert "device boot 2026-01-01T00:00:00" in text


def test_a_start_based_anchor_would_be_wrong_for_this_fixture() -> None:
    # The fixture opens with build output, exactly like the real capture whose
    # START-based anchor landed 35 minutes early. Nothing may depend on it.
    satellite = (FIXTURE_DIR / "satellite-soak.log").read_text().splitlines()
    backend = (FIXTURE_DIR / "backend.log").read_text().splitlines()

    boot, _spread = derive_anchor(satellite, backend)

    first_uptime_ms = 26  # the "I (26) boot:" line, not the acceptance event
    start_plus_uptime = BOOT + __import__("datetime").timedelta(
        milliseconds=first_uptime_ms
    )
    assert boot == BOOT
    assert boot != start_plus_uptime


def test_derive_anchor_rejects_an_inconsistent_log_pair() -> None:
    satellite = ["I (2000) ws: connected", "I (8000) ws: connected"]
    backend = [
        '2026-01-01T00:00:02.000000001Z INFO: 1 - "WebSocket /ws/voice" [accepted]',
        # 60 s later than the uptime implies: not the same events.
        '2026-01-01T00:02:08.000000001Z INFO: 1 - "WebSocket /ws/voice" [accepted]',
    ]

    with pytest.raises(LogError, match="anchor unstable"):
        derive_anchor(satellite, backend)


def test_the_anchor_tolerance_is_the_documented_one() -> None:
    # docs/soak.md tells an operator that a spread above 2 s is a hard error.
    assert ANCHOR_TOLERANCE_S == 2.0


def test_session_numbers_match_the_fixture() -> None:
    _text, payload = report(FIXTURE_DIR, UNTIL)
    sessions = _rows_dict(payload, "Sessions")

    assert sessions["device sessions ended"] == "3"
    assert sessions["server sessions opened"] == "3"
    assert sessions["server sessions closed"] == "3"
    assert sessions["loss signature"] == "3 transport_poll_write"
    assert sessions["no close frame received"] == "0"
    assert sessions["mean"] == "602 s"
    assert sessions["p50"] == "5 s"
    assert sessions["p90"] == "1439 s"
    assert sessions["max"] == "1798 s"
    assert sessions["distribution"] == "2 <10 s, 1 >=1790 s"
    assert sessions["reconciliation"] == "3 closes vs 3 ends"


def test_the_gate_numbers_and_buckets_match_the_fixture() -> None:
    _text, payload = report(FIXTURE_DIR, UNTIL)
    gate = _rows_dict(payload, "Wake word gate")

    assert gate["detections"] == "3"
    assert gate["score p50"] == "0.97"
    assert gate["score below 0.60"] == "1"
    assert gate["accepted turns"] == "2"
    assert gate["one-breath turns"] == "1 (50%)"
    assert gate["rejections"] == "5"
    # One rejection per bucket, and the empty bucket is omitted rather than
    # printed as 0 — the fixture's 0.5-6 s case does not exist on purpose.
    assert gate["  never armed"] == "1"
    assert gate["  >60 s"] == "1"
    assert gate["  10-60 s"] == "1"
    assert gate["  6-10 s"] == "1"
    assert gate["  <=0.5 s"] == "1"
    assert "  0.5-6 s" not in gate


def test_latency_numbers_match_the_fixture() -> None:
    _text, payload = report(FIXTURE_DIR, UNTIL)
    latency = _rows_dict(payload, "Latency")

    assert latency["turns with timings"] == "2"
    assert latency["graph_ttft_ms"] == "p50 3000 · p90 3800 · max 4000 ms"
    assert latency["first audio > 3 s"] == "2 (100%)"


def test_playback_and_device_numbers_match_the_fixture() -> None:
    _text, payload = report(FIXTURE_DIR, UNTIL)
    playback = _rows_dict(payload, "Playback")
    device = _rows_dict(payload, "Device")

    assert playback["replies with audio"] == "2"
    assert playback["size p50"] == "24000 B (0.8 s)"
    assert playback["ring full (truncated)"] == "1 (50%)"
    assert playback["underruns"] == "1"
    assert device["LED transitions"] == "3 IDLE, 3 LISTENING, 1 THINKING, 1 SPEAKING"
    assert device["transcript frames"] == "1"


def test_asgi_exceptions_are_counted_per_session_not_per_class_name() -> None:
    # The real soak's traceback contains "ClientDisconnected" twice per session;
    # counting the class name doubled the metric. This is the definition that
    # survives that: one "Exception in ASGI application" per failed close.
    _text, payload = report(FIXTURE_DIR, UNTIL)
    errors = _rows_dict(payload, "Server errors")

    assert errors["ASGI exceptions"].startswith("1 ")


def test_a_section_with_no_source_lines_says_n_a_instead_of_zero(
    tmp_path: Path,
) -> None:
    (tmp_path / "satellite-soak.log").write_text(
        "I (2000) ws: connected\nI (3000) transport_ws: Error transport_poll_write\n",
        encoding="utf-8",
    )
    (tmp_path / "backend.log").write_text(
        '2026-01-01T00:00:02.000000001Z INFO: 1 - "WebSocket /ws/voice" [accepted]\n',
        encoding="utf-8",
    )

    text, payload = report(tmp_path, UNTIL)
    gate = _rows_dict(payload, "Wake word gate")

    assert gate["detections"] == "n/a (0 matching lines)"
    assert "n/a (0 matching lines)" in text


def test_main_exits_non_zero_when_a_section_has_no_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["soak_report", "--dir", str(tmp_path), "--until", "2026-01-02T00:00:00Z"],
    )
    (tmp_path / "satellite-soak.log").write_text(
        "I (2000) ws: connected\nI (3000) transport_ws: Error transport_poll_write\n",
        encoding="utf-8",
    )
    (tmp_path / "backend.log").write_text(
        '2026-01-01T00:00:02.000000001Z INFO: 1 - "WebSocket /ws/voice" [accepted]\n',
        encoding="utf-8",
    )

    import scripts.soak_report as module

    assert module.main() == 1
    assert "no source lines" in capsys.readouterr().err


def test_compare_refuses_reports_from_different_windows() -> None:
    _text, current = report(FIXTURE_DIR, UNTIL)
    _later_text, later = report(FIXTURE_DIR, datetime(2026, 1, 3, tzinfo=UTC))

    code, message = compare(current, later)

    assert code == 1
    assert "NOT COMPARABLE" in message
    assert "until" in message


def test_compare_accepts_two_identical_reports() -> None:
    _text, payload = report(FIXTURE_DIR, UNTIL)
    code, message = compare(payload, payload)
    assert code == 0
    assert "Comparison" in message


def test_the_written_report_is_json_and_carries_no_duration_samples(
    tmp_path: Path,
) -> None:
    from scripts.soak_report import main as _main  # noqa: F401 - import check

    _text, payload = report(FIXTURE_DIR, UNTIL)
    encoded = json.dumps(payload)

    assert len(encoded) < 20_000, "report.json must stay diffable"
    assert "durations" not in payload["numbers"]["sessions"]
    assert payload["meta"]["percentile_rule"] == "linear, (n-1) basis"


def _rows_dict(payload: dict, section: str) -> dict[str, str]:
    """Index a payload section by label (the rendered rows are triples)."""
    return {
        label: value
        for label, value, _denominator in payload["sections"][section]
        if label
    }


def test_gate_rejections_ignore_lines_outside_the_window() -> None:
    backend = [
        '2026-01-01T00:00:02.000000001Z INFO: 1 - "WebSocket /ws/voice" [accepted]',
        '2025-12-31T23:00:00.000000001Z {"message": "ignored: no wake word (speech=1.0s, since_arm=never)"}',
    ]
    section, data = build_gate(backend, BOOT, UNTIL, 0.5)

    assert data["rejected"] == 0
    assert _rows(section)["rejections"] == "n/a (0 matching lines)"


def _installed_german_locale() -> str:
    """Return the German locale this libc actually has, or skip.

    `de_DE.UTF-8` and `de_DE.utf8` are both in the wild, and glibc silently
    falls back to C for a name it does not know — which would make the
    comparison below vacuous instead of informative.
    """
    listing = subprocess.run(
        ["locale", "-a"], capture_output=True, text=True, check=False
    ).stdout
    available = {line.strip().replace("-", "").lower() for line in listing.splitlines()}
    for candidate in ("de_DE.UTF-8", "de_DE.utf8"):
        if candidate.replace("-", "").lower() in available:
            return candidate
    pytest.skip("no German locale installed, so there is nothing to compare against")


def test_the_report_is_identical_under_a_german_locale(tmp_path: Path) -> None:
    # The shell one-liners this tool replaced printed zeros in `de_DE`, because
    # `sort -n` wants a decimal comma there. Python's parsing and formatting are
    # locale-independent, so this run must not move a single digit; the test
    # exists to catch a future refactor that shells out or uses `locale.atof`.
    german = _installed_german_locale()
    script = Path(__file__).resolve().parents[1] / "scripts" / "soak_report.py"
    out = tmp_path / "report.json"

    def run_as(locale: str) -> str:
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "--dir",
                str(FIXTURE_DIR),
                "--until",
                "2026-01-02T00:00:00Z",
                "--out",
                str(out),
            ],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            env={
                **os.environ,
                "LC_ALL": locale,
                "LC_NUMERIC": locale,
                "LANG": locale,
                "PYTHONPATH": str(script.parents[1]),
            },
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert run_as("C") == run_as(german)
