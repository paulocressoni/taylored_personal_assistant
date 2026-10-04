"""Tests for voice-turn stage timing."""

import logging

import pytest

from app.core.timing import StageMarks, log_stage_marks, monotonic_now


def _marks(**stages: float) -> StageMarks:
    """Return marks whose named stages hold the given clock readings."""
    marks = StageMarks()
    for stage, reading in stages.items():
        setattr(marks, stage, reading)
    return marks


def test_a_turn_with_no_marks_measures_nothing() -> None:
    assert StageMarks().deltas_ms() == {}
    assert StageMarks().format_marks() == ""


def test_each_stage_is_measured_between_its_own_marks() -> None:
    marks = _marks(
        speech_end=10.0,
        stt_done=10.2,
        llm_first_token=10.65,
        tts_first_byte=10.93,
        done=12.4,
    )

    assert marks.deltas_ms() == {
        "stt_ms": 200.0,
        "graph_ttft_ms": 450.0,
        "tts_ttfb_ms": 280.0,
        "first_audio_ms": 930.0,
        "tail_ms": 1470.0,
        "turn_ms": 2400.0,
    }


def test_the_product_target_spans_speech_end_to_the_first_audio_byte() -> None:
    marks = _marks(speech_end=100.0, tts_first_byte=100.94)

    assert marks.deltas_ms()["first_audio_ms"] == 940.0


def test_a_stage_that_did_not_run_is_absent_rather_than_zero() -> None:
    # A barge-in or a provider error leaves the later marks unset; reporting
    # 0 ms for them would drag every average down.
    marks = _marks(speech_end=1.0, stt_done=1.3)

    assert marks.deltas_ms() == {"stt_ms": 300.0}


def test_the_tail_is_absent_when_the_turn_never_reported_done() -> None:
    marks = _marks(speech_end=0.0, tts_first_byte=1.0)

    assert "tail_ms" not in marks.deltas_ms()
    assert "turn_ms" not in marks.deltas_ms()


def test_durations_are_rounded_to_a_tenth_of_a_millisecond() -> None:
    # Binary floats cannot hold 0.2001 exactly; the rounding is what makes the
    # log line and the trace value identical for the same turn.
    marks = _marks(speech_end=0.0, stt_done=0.2001000000001)

    assert marks.deltas_ms()["stt_ms"] == 200.1


def test_format_marks_is_one_flat_ordered_line() -> None:
    marks = _marks(speech_end=0.0, stt_done=0.2, llm_first_token=0.65)

    assert marks.format_marks() == "stt_ms=200.0ms graph_ttft_ms=450.0ms"


def test_log_stage_marks_writes_exactly_one_info_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marks = _marks(speech_end=0.0, stt_done=0.2)

    with caplog.at_level(logging.INFO):
        log_stage_marks(logging.getLogger("app.voice.session"), marks)

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.INFO
    assert caplog.records[0].getMessage() == "voice turn timing stt_ms=200.0ms"


def test_monotonic_now_never_goes_backwards() -> None:
    first = monotonic_now()
    second = monotonic_now()

    assert isinstance(first, float)
    assert second >= first
