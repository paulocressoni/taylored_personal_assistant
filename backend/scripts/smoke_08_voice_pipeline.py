"""smoke_08_voice_pipeline.py — a fixture WAV in, a spoken WAV out, no socket.

Empirical goal: run the PRODUCTION turn loop (real Silero VAD, real Groq
transcription, the real graph, real OpenAI speech) against one fixture and print
where the latency budget went. Only the transport is replaced, so the stages
measured here are the stages the socket measures.

The per-stage timings are the session's own log line; the script adds the one
number the session cannot know — the wait from the last speech frame it sent to
the first audio byte that came back.

Usage (from backend/, with the voice keys in .env.dev and the fixtures generated):
    uv run python scripts/smoke_08_voice_pipeline.py --lang en
    uv run python scripts/smoke_08_voice_pipeline.py --lang de --out de_reply.wav
"""

import argparse
import asyncio
import logging
import math
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.core.timing import monotonic_now
from app.graph.graph import build_graph
from app.voice.audio import (
    SAMPLE_WIDTH_BYTES,
    duration_s,
    iter_frames,
    read_wav,
    wrap_wav,
)
from app.voice.registry import build_voice_providers
from app.voice.session import VoiceSession

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
FRAME_MS = 20  # what the browser worklet sends
TURN_TIMEOUT_S = 60.0


class _Recorder:
    """Stands in for the socket: keeps the audio, signals the end of the turn."""

    def __init__(self) -> None:
        self.audio = bytearray()
        self.events: list[str] = []
        self.speech_sent_at: float | None = None
        self.first_audio_at: float | None = None
        self.turn_done = asyncio.Event()

    async def __call__(self, frame: bytes | dict[str, Any]) -> None:
        """Record one outbound frame, exactly as the socket adapter would send it."""
        if isinstance(frame, bytes):
            if not self.audio:
                self.first_audio_at = monotonic_now()
            self.audio.extend(frame)
            return
        self.events.append(frame["type"])
        if frame["type"] == "transcript":
            print(f"  transcript    : {frame['text']}")
        elif frame["type"] == "error":
            print(f"  error         : {frame['detail']}")
        elif frame["type"] == "audio_end":
            self.turn_done.set()


async def _frames(
    pcm: bytes, recorder: _Recorder
) -> AsyncIterator[bytes | dict[str, Any]]:
    """Feed one utterance, then hold the mic open until the answer is done.

    A real microphone never stops: the endpointer needs silence to close the
    segment, and the client keeps capturing while the answer plays — which is
    also what makes barge-in possible at all.
    """
    frame_bytes = (
        FRAME_MS * settings.voice_input_sample_rate // 1000 * SAMPLE_WIDTH_BYTES
    )
    for frame in iter_frames(pcm, frame_bytes):
        yield frame
    recorder.speech_sent_at = monotonic_now()
    # iter_frames drops a partial trailing frame: at most 20 ms of the prompt,
    # which the endpointer's speech pad covers.
    silence = b"\x00" * frame_bytes
    for _ in range(math.ceil((settings.voice_vad_min_silence_ms + 300) / FRAME_MS)):
        yield silence
    try:
        await asyncio.wait_for(recorder.turn_done.wait(), timeout=TURN_TIMEOUT_S)
    except TimeoutError:
        print("  the turn never reported audio_end")
    yield {"type": "stop"}


async def run(lang: str, out: Path) -> None:
    """Drive one fixture through the real session and write the reply as a WAV.

    Args:
        lang: Which fixture to speak ("en", "de" or "pt").
        out: Where to write the reply WAV.

    Raises:
        SystemExit: if the fixture, the voice config or the reply is missing.
    """
    fixture = FIXTURE_DIR / f"voice_{lang}.wav"
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

    recorder = _Recorder()
    session = VoiceSession(
        graph=build_graph(),
        transcriber=providers.transcriber,
        synthesizer=providers.synthesizer,
        vad=providers.vad_factory(),
        send=recorder,
        session_id=f"smoke08-{lang}",
        device_id="smoke08",
        # The reSpeaker contract: 16 kHz both ways, so the reply WAV comes out at
        # the fixture's own rate and the resampler runs exactly as it will on the
        # hardware path.
        output_sample_rate=rate,
    )

    print(f"fixture       : {fixture.name} ({duration_s(pcm, rate):.2f}s @ {rate} Hz)")
    await session.run(_frames(pcm, recorder))
    if not recorder.audio:
        raise SystemExit("no audio came back — see the log above")

    reply = bytes(recorder.audio)
    out.write_bytes(wrap_wav(reply, rate))
    print(f"frames        : {' '.join(recorder.events)}")
    print(f"reply audio   : {duration_s(reply, rate):.2f}s -> {out}")
    if recorder.speech_sent_at is not None and recorder.first_audio_at is not None:
        perceived_ms = (recorder.first_audio_at - recorder.speech_sent_at) * 1000.0
        print(
            f"perceived     : {perceived_ms:.0f} ms (last speech frame sent -> first "
            f"audio byte; includes the {settings.voice_vad_min_silence_ms} ms of "
            "silence the endpointer waits for)"
        )


def main() -> None:
    """Parse the CLI arguments and run one turn."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="en", choices=["en", "de", "pt"])
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where to write the reply WAV (default: the temp directory)",
    )
    args = parser.parse_args()
    # INFO is what makes the session's own "voice turn timing" line visible.
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    out = args.out or Path(tempfile.gettempdir()) / f"voice_smoke08_{args.lang}.wav"
    asyncio.run(run(args.lang, out))


async def test_smoke_08_voice_pipeline() -> None:
    """One English fixture turn, through the real providers."""
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    await run("en", Path(tempfile.gettempdir()) / "voice_smoke08_test.wav")


if __name__ == "__main__":
    main()
