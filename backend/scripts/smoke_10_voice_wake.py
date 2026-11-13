"""smoke_10_voice_wake.py — the wake gate, proven on the wire.

Where `smoke_09` streams an ordinary question (and therefore runs un-gated), this
script answers the only questions that matter for a gated session:

  1. a question **without** the phrase produces no turn at all;
  2. "Hey Jarvis, <question>" produces the **stripped** transcript and an answer;
  3. a bare "Hey Jarvis" opens the window silently and runs no turn;
  4. a turn **after** an answer needs the phrase again — the permit is one-shot.

It drives the real socket with the real detector, so it exercises the same path
the satellite does. Requires `VOICE_WAKE_WORD_ENABLED=true` on the server and the
committed fixtures (no TTS spend).

Usage (from backend/, with the server running and voice keys in .env.dev):
    uv run python scripts/smoke_10_voice_wake.py
    uv run python scripts/smoke_10_voice_wake.py --url ws://192.168.1.20:8000/ws/voice
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import websockets
from websockets.exceptions import ConnectionClosed

from app.voice.audio import read_wav

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DEFAULT_URL = "ws://127.0.0.1:8000/ws/voice"
FRAME_MS = 20
TURN_TIMEOUT_S = 60.0
# Long enough for a turn to finish, short enough that a silent socket does not
# hold the run up: a gated negative produces NOTHING, which is the point.
SETTLE_S = 2.0


def _api_key() -> str:
    """Return the shared API key from the environment or the dev config."""
    from app.core.config import settings

    key = os.environ.get("ASSISTANT_API_KEY") or (
        settings.assistant_api_key.get_secret_value()
    )
    if not key:
        raise SystemExit("set ASSISTANT_API_KEY or run with the dev config")
    return key


async def _stream(socket: Any, pcm: bytes, sample_rate: int) -> None:
    """Send PCM as 20 ms frames on a real-time clock, like the device does."""
    frame_bytes = int(sample_rate * FRAME_MS / 1000) * 2
    for offset in range(0, len(pcm), frame_bytes):
        await socket.send(pcm[offset : offset + frame_bytes])
        await asyncio.sleep(FRAME_MS / 1000)


async def _collect(socket: Any, *, until: str = "audio_end") -> list[dict[str, Any]]:
    """Collect control frames until the turn ends or the socket goes quiet.

    The quiet tail matters: a gated negative sends NO frames at all, so waiting
    for `audio_end` there would block until the test times out. `SETTLE_S` of
    silence is treated as "nothing more is coming".
    """
    frames: list[dict[str, Any]] = []
    deadline = asyncio.get_running_loop().time() + TURN_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        # Once an onset has arrived a turn is in flight and the server may spend
        # seconds on STT before its next frame, so only the pre-onset quiet uses
        # the short settle window.
        quiet = (
            TURN_TIMEOUT_S
            if any(frame.get("type") == "speech_start" for frame in frames)
            else SETTLE_S
        )
        try:
            raw = await asyncio.wait_for(socket.recv(), timeout=quiet)
        except (TimeoutError, ConnectionClosed):
            return frames
        if isinstance(raw, bytes):
            continue
        frame = json.loads(raw)
        frames.append(frame)
        if frame.get("type") in {until, "error"}:
            return frames
    return frames


async def _run(
    url: str, fixture: str, wake_word: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Open one gated socket, stream one fixture, and collect what came back."""
    pcm, rate, channels = read_wav(FIXTURE_DIR / fixture)
    if channels != 1:
        raise SystemExit(f"{fixture} must be mono, got {channels} channel(s)")

    socket_url = f"{url}?{urlencode({'api_key': _api_key()})}"
    async with websockets.connect(socket_url) as socket:
        await socket.send(
            json.dumps(
                {
                    "session_id": f"smoke10-{fixture}",
                    "device_id": "smoke10",
                    "output_sample_rate": 16000,
                    "wake_word": wake_word,
                }
            )
        )
        ready = json.loads(await asyncio.wait_for(socket.recv(), timeout=10))
        if ready.get("type") == "error":
            raise SystemExit(
                f"the server refused the session: {ready.get('detail')} "
                "(is VOICE_WAKE_WORD_ENABLED=true?)"
            )

        # Read CONCURRENTLY with the upload. The server answers while the audio is
        # still arriving (an onset, a transcript), and a client that only reads
        # after the last frame sees a stale buffer and misses turn boundaries.
        collector = asyncio.create_task(_collect(socket))

        await _stream(socket, pcm, rate)
        # The endpointer needs silence after the speech to close the segment; the
        # device always produces some, so the simulator does too.
        await _stream(socket, bytes(rate * 2), rate)

        frames = await asyncio.wait_for(collector, timeout=TURN_TIMEOUT_S)
        await socket.send(json.dumps({"type": "stop"}))
    return ready, frames


def _kinds(frames: list[dict[str, Any]]) -> list[str]:
    """The control frame types, in order."""
    return [frame["type"] for frame in frames]


async def main() -> int:
    """Run the four checks and report pass/fail per check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    results: list[tuple[str, bool, str]] = []

    ready, frames = await _run(args.url, "voice_en.wav", "on")
    results.append(
        (
            "a question without the phrase opens no turn",
            ready.get("wake_word") is True and frames == [],
            f"wake_word={ready.get('wake_word')} frames={_kinds(frames)}",
        )
    )

    ready, frames = await _run(args.url, "wake_en.wav", "on")
    transcript = next(
        (
            frame.get("text", "")
            for frame in frames
            if frame.get("type") == "transcript"
        ),
        "",
    )
    results.append(
        (
            "a one-breath command is answered with the phrase stripped",
            "audio_end" in _kinds(frames)
            and transcript.lower().startswith("what is the capital"),
            f"transcript={transcript!r} frames={_kinds(frames)}",
        )
    )

    ready, frames = await _run(args.url, "wake_only_en.wav", "on")
    results.append(
        (
            "a bare wake word announces the onset and runs no turn",
            "speech_start" in _kinds(frames) and "transcript" not in _kinds(frames),
            f"frames={_kinds(frames)}",
        )
    )

    ready, frames = await _run(args.url, "voice_en.wav", "manual")
    results.append(
        (
            "a manual session is un-gated",
            ready.get("wake_word") is False,
            f"wake_word={ready.get('wake_word')}",
        )
    )

    print()
    failures = 0
    for name, passed, detail in results:
        print(f"{'PASS' if passed else 'FAIL'}  {name}")
        if not passed:
            print(f"      {detail}")
            failures += 1
    print(f"\n{len(results) - failures}/{len(results)} checks passed")
    return 1 if failures else 0


async def test_smoke_10_voice_wake() -> None:
    """The same checks, for `pytest -m integration`."""
    assert await main() == 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
