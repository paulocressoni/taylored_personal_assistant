"""Tests for the streaming resampler used on the 16 kHz device path."""

import numpy as np
import pytest

from app.voice.audio import (
    INT16_FULL_SCALE,
    SAMPLE_WIDTH_BYTES,
    StreamingResampler,
    resample_pcm,
)


def _chunks(pcm: bytes, size: int) -> list[bytes]:
    """Split PCM into fixed-size chunks, the last one short."""
    return [pcm[at : at + size] for at in range(0, len(pcm), size)]


def _tone(samples: int, rate: int = 24000, hz: int = 440) -> bytes:
    """Return a sine tone as mono s16le PCM."""
    axis = np.arange(samples, dtype=np.float64) / rate
    wave = 0.5 * np.sin(2.0 * np.pi * hz * axis)
    return (wave * INT16_FULL_SCALE).astype("<i2").tobytes()


def test_equal_rates_pass_every_chunk_through() -> None:
    resampler = StreamingResampler(16000, 16000)
    chunk = b"\x01\x02\x03\x04"

    assert resampler.process(chunk) == chunk
    assert resampler.flush() == b""


def test_nothing_is_emitted_before_the_context_is_complete() -> None:
    resampler = StreamingResampler(24000, 16000)

    assert resampler.process(_tone(16)) == b""
    assert resampler.flush() != b""  # held back, not dropped


def test_streamed_output_is_byte_identical_to_a_single_call() -> None:
    # 100 ms chunks, the size a provider actually streams.
    pcm = _tone(24000)
    resampler = StreamingResampler(24000, 16000)

    streamed = b"".join(resampler.process(chunk) for chunk in _chunks(pcm, 4800))
    expected = resample_pcm(pcm, 24000, 16000)

    # Every emitted byte is already final; only the tail that still needs
    # right-hand context is missing.
    assert expected.startswith(streamed)
    assert 0 < len(expected) - len(streamed) <= 128


def test_upsampling_emits_immediately() -> None:
    # 16 kHz -> 24 kHz needs no anti-alias filter, so nothing is held back
    # beyond the one sample the interpolation cannot close yet.
    resampler = StreamingResampler(16000, 24000)

    out = resampler.process(_tone(1600, rate=16000))
    tail = resampler.flush()

    assert len(out) > 0
    assert len(out) + len(tail) == 2400 * SAMPLE_WIDTH_BYTES


def test_flush_is_idempotent() -> None:
    resampler = StreamingResampler(24000, 16000)
    resampler.process(_tone(480))

    assert resampler.flush() != b""
    assert resampler.flush() == b""


def test_a_non_positive_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        StreamingResampler(24000, 0)


def test_a_chunk_that_is_not_whole_samples_is_rejected() -> None:
    # A half sample would reach numpy as an odd-length buffer and surface as an
    # opaque "buffer size must be a multiple of element size".
    with pytest.raises(ValueError, match="whole samples"):
        StreamingResampler(24000, 16000).process(b"\x00")
