"""voice_latency.py — N warm turns, p50/p90 per stage.

Empirical goal: answer "does the voice path meet its budget?" with numbers that
mean something. Two things make that possible:

  - every run happens in ONE process, so the TLS connections to Groq, DeepSeek and
    OpenAI stay warm and the first run does not masquerade as the pipeline's speed;
  - frames are paced at capture rate, so the endpointer's silence window really
    elapses. Push the frames as fast as the event loop allows and the perceived
    figure silently loses VOICE_VAD_MIN_SILENCE_MS.

The first stages come from the session itself; `perceived_ms` is measured here,
from the frame clock, because end-of-speech to first audio byte is the form the
target is written in and the session cannot see the client's last speech frame.

Usage (from backend/, with the voice keys in .env.dev):
    uv run python scripts/voice_latency.py
    uv run python scripts/voice_latency.py --lang de --runs 10 --warmup 2
"""

import argparse
import asyncio
import logging
import math
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.observability import flush
from app.core.timing import monotonic_now
from app.graph.graph import build_graph
from app.voice.audio import SAMPLE_WIDTH_BYTES, duration_s, iter_frames, read_wav
from app.voice.registry import build_voice_providers
from app.voice.session import VoiceSession

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FRAME_MS = 20
TURN_TIMEOUT_S = 60.0
# The M29 acceptance criterion, in the metric it is written in.
TARGET_MS = 1500.0

# The session's own stages, in the order they happen.
MEASURED = (
    "stt_ms",
    "graph_ttft_ms",
    "tts_ttfb_ms",
    "first_audio_ms",
    "tail_ms",
    "turn_ms",
)
# `perceived_ms`, `generation_ms` and `tts_call_ms` are measured HERE, from the
# frame clock, because the session cannot see the client's last speech frame or
# the moment the reply stopped streaming. The last two split `tts_ttfb_ms`, which
# otherwise hides the reply's own generation time whenever the first sentence
# ends the reply — which is exactly what a one-sentence answer does.
#
# The per-run row carries the stages plus `perceived_ms`; the two diagnostics are
# summary-only, so the table still fits a terminal.
ROW_METRICS = MEASURED + ("perceived_ms",)
SUMMARY_METRICS = ROW_METRICS + ("generation_ms", "tts_call_ms")


class _Recorder:
    """Stands in for the socket, and holds the client's half of the clock."""

    def __init__(self) -> None:
        self.audio = bytearray()
        self.speech_sent_at: float | None = None
        self.first_token_at: float | None = None
        self.last_token_at: float | None = None
        self.first_audio_at: float | None = None
        self.turn_done = asyncio.Event()

    async def __call__(self, frame: bytes | dict[str, Any]) -> None:
        """Record one outbound frame from the session."""
        if isinstance(frame, bytes):
            if not self.audio:
                self.first_audio_at = monotonic_now()
            self.audio.extend(frame)
        elif frame["type"] == "token":
            # The reply's own clock. Stamped here rather than in the session
            # because only the frame stream knows when generation stopped.
            if self.first_token_at is None:
                self.first_token_at = monotonic_now()
            self.last_token_at = monotonic_now()
        elif frame["type"] == "audio_end":
            self.turn_done.set()

    @property
    def perceived_ms(self) -> float | None:
        """Last speech frame sent -> first audio byte, or None if unmeasurable."""
        if self.speech_sent_at is None or self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.speech_sent_at) * 1000.0

    @property
    def generation_ms(self) -> float | None:
        """First token -> last token: how long the reply took to stream."""
        if self.first_token_at is None or self.last_token_at is None:
            return None
        return (self.last_token_at - self.first_token_at) * 1000.0

    @property
    def tts_call_ms(self) -> float | None:
        """Last token -> first audio byte: the TTS round trip, reply in hand.

        Only meaningful when the reply did not stream a speakable sentence before
        its last token; for a multi-sentence reply this can go negative, because
        the first sentence was already being spoken while the rest generated.
        """
        if self.last_token_at is None or self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.last_token_at) * 1000.0


async def _sleep_until(deadline: float) -> None:
    """Sleep until an absolute monotonic deadline, so pacing cannot drift."""
    delay = deadline - monotonic_now()
    if delay > 0:
        await asyncio.sleep(delay)


async def _frames(
    pcm: bytes, rate: int, recorder: _Recorder
) -> AsyncIterator[bytes | dict[str, Any]]:
    """Replay the fixture at capture rate, then hold the mic open for the answer."""
    frame_bytes = FRAME_MS * rate // 1000 * SAMPLE_WIDTH_BYTES
    started = monotonic_now()
    sent = 0
    for frame in iter_frames(pcm, frame_bytes):
        sent += 1
        await _sleep_until(started + sent * FRAME_MS / 1000.0)
        yield frame
    recorder.speech_sent_at = monotonic_now()
    silence = b"\x00" * frame_bytes
    pads = math.ceil((settings.voice_vad_min_silence_ms + 200) / FRAME_MS)
    for _ in range(pads):
        sent += 1
        await _sleep_until(started + sent * FRAME_MS / 1000.0)
        yield silence
    try:
        await asyncio.wait_for(recorder.turn_done.wait(), timeout=TURN_TIMEOUT_S)
    except TimeoutError:
        print("  the turn never reported audio_end")
    yield {"type": "stop"}


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: the smallest value covering `fraction` of runs.

    Nearest rank rather than interpolation: with a handful of runs a latency
    number that was never observed is not a useful p90.
    """
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def _run_line(index: int, values: dict[str, float], warmup: bool) -> str:
    """Render one run as a table row."""
    cells = "  ".join(
        f"{values.get(name, float('nan')):>13.1f}" for name in ROW_METRICS
    )
    return f"{index:>3}  {cells}" + ("   (warm-up, discarded)" if warmup else "")


def _summary(samples: dict[str, list[float]]) -> str:
    """Render the p50/p90 table for every metric that was measured."""
    width = max(len(name) for name in SUMMARY_METRICS)
    lines = [f"{'metric':<{width}}  {'p50':>9}  {'p90':>9}"]
    for name in SUMMARY_METRICS:
        values = samples[name]
        if not values:
            continue
        lines.append(
            f"{name:<{width}}  {_percentile(values, 0.5):>9.1f}"
            f"  {_percentile(values, 0.9):>9.1f}"
        )
    return "\n".join(lines)


async def measure(args: argparse.Namespace) -> None:
    """Drive the fixture through the real session `args.runs` times and report.

    Args:
        args: Parsed CLI arguments.

    Raises:
        SystemExit: if the fixture or the voice configuration is missing.
    """
    fixture = FIXTURE_DIR / f"voice_{args.lang}.wav"
    if not fixture.exists():
        raise SystemExit(
            f"missing {fixture} — generate it first: "
            "uv run python scripts/make_voice_fixtures.py"
        )
    providers = build_voice_providers()
    if providers is None:
        raise SystemExit(
            "voice is not configured: set VOICE_ENABLED=true and both keys"
        )

    pcm, rate, channels = read_wav(fixture)
    if channels != 1 or rate != settings.voice_input_sample_rate:
        raise SystemExit(
            f"{fixture.name} must be mono at {settings.voice_input_sample_rate} Hz, "
            f"got {channels} channel(s) at {rate} Hz"
        )

    # Built ONCE, outside the loop. This is the whole reason the tool exists: a
    # fresh provider client pays three TLS handshakes before it does any work.
    graph = build_graph()
    samples: dict[str, list[float]] = {name: [] for name in SUMMARY_METRICS}
    total = args.warmup + args.runs

    print(f"fixture  : {fixture.name} ({duration_s(pcm, rate):.2f}s @ {rate} Hz)")
    print(f"schedule : {total} runs, first {args.warmup} discarded as warm-up")
    print()
    print(f"{'run':>3}  " + "  ".join(f"{name:>13}" for name in ROW_METRICS))
    try:
        for index in range(1, total + 1):
            recorder = _Recorder()
            session = VoiceSession(
                graph=graph,
                transcriber=providers.transcriber,
                synthesizer=providers.synthesizer,
                # A NEW VAD per run: it carries recurrent state and 64 samples of
                # context from the previous window, so reusing one would let run N
                # hear run N-1.
                vad=providers.vad_factory(),
                send=recorder,
                session_id=f"latency-{args.lang}-{index}",
                # 16 kHz out: the hardware contract, and it keeps this comparable
                # with what the reSpeaker will actually hear.
                output_sample_rate=rate,
            )
            await session.run(_frames(pcm, rate, recorder))

            if not session.turn_timings:
                print(f"{index:>3}  (no completed turn)")
                continue
            values = dict(session.turn_timings[0])
            for name, derived in (
                ("perceived_ms", recorder.perceived_ms),
                ("generation_ms", recorder.generation_ms),
                ("tts_call_ms", recorder.tts_call_ms),
            ):
                values[name] = derived if derived is not None else float("nan")

            warmup = index <= args.warmup
            print(_run_line(index, values, warmup))
            if warmup:
                continue
            for name in SUMMARY_METRICS:
                value = values.get(name)
                if value is not None and not math.isnan(value):
                    samples[name].append(value)
    finally:
        # Same reason app/graph/cli.py flushes: an unflushed process loses every
        # trace and logs an OTLP export timeout on the way out.
        flush()

    print()
    print(_summary(samples))
    if samples["first_audio_ms"]:
        p50 = _percentile(samples["first_audio_ms"], 0.5)
        verdict = "within budget" if p50 < TARGET_MS else "OVER BUDGET"
        print(
            f"\nfirst_audio_ms p50 = {p50:.1f} ms vs the {TARGET_MS:.0f} ms target"
            f"  [{verdict}]"
        )
    if samples["perceived_ms"]:
        p50 = _percentile(samples["perceived_ms"], 0.5)
        print(
            f"perceived_ms   p50 = {p50:.1f} ms (adds the "
            f"{settings.voice_vad_min_silence_ms} ms silence window the caller feels)"
        )


def main() -> None:
    """Parse the CLI arguments and run the measurement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="en", choices=["en", "de", "pt"])
    parser.add_argument("--runs", type=int, default=5, help="measured runs")
    parser.add_argument(
        "--warmup",
        type=int,
        default=1,
        help="runs to discard first: they pay for the TLS handshakes",
    )
    args = parser.parse_args()
    # WARNING, not INFO: the tool prints its own table, and the session's log line
    # would interleave with it.
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(measure(args))


if __name__ == "__main__":
    main()
