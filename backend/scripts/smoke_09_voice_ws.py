"""smoke_09_voice_ws.py — the ESP32 simulator: a WAV streamed over /ws/voice.

Empirical goal: exercise the socket contract with the only client we can build
today (the firmware does not exist yet), using the framing the reSpeaker XVF3800
will use — 16 kHz mono s16le up, 16 kHz mono s16le down, 20 ms frames sent on a
real-time clock rather than as fast as the loop allows.

It also reports what the device cannot: end of speech to first audio byte, from
the client's side of the wire.

The key travels as ?api_key= (a browser uses the Sec-WebSocket-Protocol header
instead; the server accepts both, and both draw on the same budget).

Usage (from backend/, with the server running and the voice keys in .env.dev):
    uv run python scripts/smoke_09_voice_ws.py --lang de
    uv run python scripts/smoke_09_voice_ws.py --lang pt --output-rate 24000
"""

import argparse
import asyncio
import json
import logging
import math
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
import websockets  # comes with uvicorn[standard]
from websockets.exceptions import ConnectionClosed

from app.core.config import settings
from app.core.timing import monotonic_now
from app.voice.audio import (
    SAMPLE_WIDTH_BYTES,
    duration_s,
    iter_frames,
    read_wav,
    wrap_wav,
)

pytestmark = pytest.mark.integration

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
DEFAULT_URL = "ws://127.0.0.1:8000/ws/voice"
FRAME_MS = 20
TURN_TIMEOUT_S = 60.0


class _Recorder:
    """The client's view of one session: frames printed, audio kept, clock read."""

    def __init__(self) -> None:
        self.audio = bytearray()
        self.speech_sent_at: float | None = None
        self.first_audio_at: float | None = None
        self.turn_done = asyncio.Event()

    def add_audio(self, chunk: bytes) -> None:
        """Keep a binary frame, noting when the first one arrived."""
        if not self.audio:
            self.first_audio_at = monotonic_now()
        self.audio.extend(chunk)

    def handle(self, frame: dict[str, Any]) -> None:
        """Print a control frame and act on the one that ends the turn."""
        kind = frame["type"]
        if kind == "ready":
            print(
                f"  ready        : {frame['input_sample_rate']} Hz in / "
                f"{frame['output_sample_rate']} Hz out ({frame['format']})"
            )
        elif kind in {"transcript", "error"}:
            print(f"  {kind:13}: {frame.get('text') or frame.get('detail')}")
        elif kind == "audio_end":
            self.turn_done.set()

    @property
    def perceived_ms(self) -> float | None:
        """Last speech frame sent -> first audio byte, or None if unmeasurable."""
        if self.speech_sent_at is None or self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.speech_sent_at) * 1000.0


async def _sleep_until(deadline: float) -> None:
    """Sleep until an absolute monotonic deadline, so pacing cannot drift."""
    delay = deadline - monotonic_now()
    if delay > 0:
        await asyncio.sleep(delay)


def _api_key() -> str:
    """The shared API key the server enforces on the WS handshake."""
    return settings.assistant_api_key.get_secret_value()


async def _stream(socket: Any, pcm: bytes, rate: int, recorder: _Recorder) -> None:
    """Send the prompt at capture rate, then the silence that closes the segment."""
    frame_bytes = FRAME_MS * rate // 1000 * SAMPLE_WIDTH_BYTES
    silence = b"\x00" * frame_bytes
    started = monotonic_now()
    sent = 0
    for frame in iter_frames(pcm, frame_bytes):
        sent += 1
        await _sleep_until(started + sent * FRAME_MS / 1000.0)
        await socket.send(frame)
    recorder.speech_sent_at = monotonic_now()
    # A device keeps capturing while the answer plays; the endpointer will not
    # close the segment until it has heard VOICE_VAD_MIN_SILENCE_MS of silence.
    for _ in range(math.ceil((settings.voice_vad_min_silence_ms + 200) / FRAME_MS)):
        sent += 1
        await _sleep_until(started + sent * FRAME_MS / 1000.0)
        await socket.send(silence)


async def _receive(socket: Any, recorder: _Recorder) -> None:
    """Collect the server's frames until the answer is complete or the socket dies."""
    try:
        async for message in socket:
            if isinstance(message, bytes):
                recorder.add_audio(message)
                continue
            frame = json.loads(message)
            recorder.handle(frame)
            if frame["type"] == "audio_end":
                return
    except ConnectionClosed:
        return


async def run(lang: str, output_rate: int, url: str, out: Path | None) -> None:
    """Stream one fixture over the socket and report what came back.

    Args:
        lang: Which fixture to speak ("en", "de" or "pt").
        output_rate: Rate to ask the server to speak at, in Hz.
        url: The voice socket URL, without the key.
        out: Where to write the reply WAV, or None to keep only the numbers.

    Raises:
        SystemExit: if the fixture is missing or nothing came back.
    """
    fixture = FIXTURE_DIR / f"voice_{lang}.wav"
    if not fixture.exists():
        raise SystemExit(
            f"missing {fixture} — generate it first: "
            "uv run python scripts/make_voice_fixtures.py"
        )
    pcm, rate, channels = read_wav(fixture)
    if channels != 1 or rate != settings.voice_input_sample_rate:
        raise SystemExit(
            f"{fixture.name} must be mono at {settings.voice_input_sample_rate} Hz, "
            f"got {channels} channel(s) at {rate} Hz"
        )

    recorder = _Recorder()
    print(f"fixture  : {fixture.name} ({duration_s(pcm, rate):.2f}s @ {rate} Hz)")
    print(f"socket   : {url} asking for {output_rate} Hz")

    async with websockets.connect(
        f"{url}?{urlencode({'api_key': _api_key()})}"
    ) as socket:
        await socket.send(
            json.dumps(
                {
                    "session_id": f"smoke09-{lang}",
                    "device_id": "smoke09",
                    "output_sample_rate": output_rate,
                }
            )
        )
        receiver = asyncio.create_task(_receive(socket, recorder))
        await _stream(socket, pcm, rate, recorder)
        try:
            await asyncio.wait_for(recorder.turn_done.wait(), timeout=TURN_TIMEOUT_S)
        except TimeoutError:
            print("  the server never reported audio_end")
        await socket.send(json.dumps({"type": "stop"}))
        # The receiver has already returned on audio_end; cancelling is how we
        # make sure of it on the timeout path too.
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

    if not recorder.audio:
        raise SystemExit("no audio came back — see the server log")
    reply = bytes(recorder.audio)
    print(f"reply    : {duration_s(reply, output_rate):.2f}s of audio")
    if out is not None:
        out.write_bytes(wrap_wav(reply, output_rate))
        print(f"written  : {out}")
    if recorder.perceived_ms is not None:
        print(
            f"perceived: {recorder.perceived_ms:.0f} ms "
            "(end of speech -> first audio byte, transport included)"
        )


def main() -> None:
    """Parse the CLI arguments and run one device-style turn."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", default="en", choices=["en", "de", "pt"])
    parser.add_argument(
        "--output-rate",
        type=int,
        default=16000,
        choices=[16000, 24000],
        help="the reSpeaker's I2S rate (16000) or the browser's 24000",
    )
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="also write the reply WAV here",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    asyncio.run(run(args.lang, args.output_rate, args.url, args.out))


async def test_smoke_09_voice_ws() -> None:
    """One device-style turn against a live server."""
    logging.basicConfig(
        level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    await run(
        "en", 16000, DEFAULT_URL, Path(tempfile.gettempdir()) / "voice_smoke09_en.wav"
    )


if __name__ == "__main__":
    main()
