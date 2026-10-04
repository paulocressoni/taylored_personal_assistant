"""Stage timing for one voice turn: where the latency budget actually went.

WHY `perf_counter` AND NOT `time.time`: the wall clock can be stepped by NTP or
by hand, which turns a duration negative or wildly large. The monotonic clock
cannot go backwards, so a measurement is always meaningful.

WHY THE MARKS ARE NOT IN IPAState: they are diagnostics, not conversation state.
Persisting them would grow every later turn's checkpoint and tie old checkpoints
to a schema for no behavioural gain, so they live for exactly one turn.

Marks are ABSOLUTE readings on the monotonic clock; durations are derived from
pairs. A stage that never ran leaves its mark unset, so its duration is ABSENT
rather than zero — an unmeasured stage must never average in as instant.

Pure module: stdlib only, so it is testable with no settings and no ENV.
"""

import logging
import time
from dataclasses import dataclass

# (metric, start mark, end mark). `first_audio_ms` is the product target
# (end-of-speech to first audio byte); the three stages before it are what tells
# you which stage blew the budget. This order is the order of the log line.
#
# `speech_end` is stamped when the endpointer CLOSES the utterance, which is
# after its trailing-silence pad, so `first_audio_ms` is optimistic by that pad
# (30 ms by default) against a human's sense of when they stopped talking.
_SPANS: tuple[tuple[str, str, str], ...] = (
    ("stt_ms", "speech_end", "stt_done"),
    ("graph_ttft_ms", "stt_done", "llm_first_token"),
    ("tts_ttfb_ms", "llm_first_token", "tts_first_byte"),
    ("first_audio_ms", "speech_end", "tts_first_byte"),
    ("tail_ms", "tts_first_byte", "done"),
    ("turn_ms", "speech_end", "done"),
)


def monotonic_now() -> float:
    """Return the current monotonic clock reading, in seconds.

    Every `StageMarks` field must come from this function, so no caller can
    accidentally measure a latency with the wall clock.

    Returns:
        Seconds from an arbitrary origin, comparable only with other readings
        from this clock in this process — never with a wall-clock time.
    """
    return time.perf_counter()


@dataclass
class StageMarks:
    """Absolute clock readings for one voice turn; stages that never ran stay None.

    Attributes:
        speech_end: The endpointer closed the utterance.
        stt_done: The transcript came back.
        llm_first_token: The first answer token arrived.
        tts_first_byte: The first audio byte was ready to send.
        done: The last audio byte was sent.
    """

    speech_end: float | None = None
    stt_done: float | None = None
    llm_first_token: float | None = None
    tts_first_byte: float | None = None
    done: float | None = None

    def deltas_ms(self) -> dict[str, float]:
        """Return the measured stage durations in milliseconds.

        Returns:
            Metric name -> milliseconds, rounded to 0.1 ms. Metrics whose start
            or end mark is unset are omitted, so a barge-in or a provider error
            can never be mistaken for an instant stage.
        """
        deltas: dict[str, float] = {}
        for metric, start, end in _SPANS:
            begin = getattr(self, start)
            finish = getattr(self, end)
            if begin is None or finish is None:
                continue
            deltas[metric] = round((finish - begin) * 1000.0, 1)
        return deltas

    def format_marks(self) -> str:
        """Render the measured stages as one flat `name=value` line.

        Returns:
            Space-separated `name=value.ms` pairs, empty when nothing was
            measured. Flat and short on purpose: it is one log line, and one
            scalar per metric once it reaches the trace.
        """
        return " ".join(
            f"{metric}={value:.1f}ms" for metric, value in self.deltas_ms().items()
        )


def log_stage_marks(logger: logging.Logger, marks: StageMarks) -> None:
    """Log one voice turn's stage timings as a single INFO line.

    Args:
        logger: The calling module's logger.
        marks: The finished turn's stage marks.
    """
    logger.info("voice turn timing %s", marks.format_marks())
