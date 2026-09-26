"""Unit tests for the pure PCM helpers in app.voice.audio."""

import io
import wave

import numpy as np
import pytest

from app.voice.audio import (
    SAMPLE_WIDTH_BYTES,
    duration_s,
    int16_view,
    iter_frames,
    resample_pcm,
    rms_level,
    sample_count,
    to_float32,
    to_pcm16,
    wrap_wav,
)


def _sine(
    freq_hz: float,
    sample_rate: int,
    seconds: float,
    amplitude: float = 0.5,
) -> bytes:
    """Build a PCM sine tone, the cheapest signal with a known frequency."""
    time = np.arange(round(sample_rate * seconds), dtype=np.float64) / sample_rate
    wave_samples = amplitude * np.sin(2 * np.pi * freq_hz * time)
    return to_pcm16(wave_samples.astype(np.float32))


# --- sample_count / duration_s ----------------------------------------------


def test_sample_count_counts_int16_samples() -> None:
    assert sample_count(b"") == 0
    assert sample_count(b"\x00\x00") == 1
    assert sample_count(np.zeros(512, dtype="<i2").tobytes()) == 512


def test_sample_count_ignores_an_odd_trailing_byte() -> None:
    assert sample_count(b"\x00\x00\x00") == 1


def test_duration_s_divides_samples_by_rate() -> None:
    pcm = np.zeros(16000, dtype="<i2").tobytes()
    assert duration_s(pcm, 16000) == pytest.approx(1.0)
    assert duration_s(pcm, 8000) == pytest.approx(2.0)


def test_duration_s_rejects_a_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="sample_rate"):
        duration_s(b"\x00\x00", 0)


# --- int16 <-> float32 ------------------------------------------------------


def test_int16_view_is_a_read_only_zero_copy_view() -> None:
    pcm = np.array([0, 1, -1, 32767, -32768], dtype="<i2").tobytes()
    view = int16_view(pcm)
    assert view.shape == (5,)
    assert not view.flags.writeable


def test_to_float32_maps_the_extremes_to_one() -> None:
    pcm = np.array([0, 32767, -32768], dtype="<i2").tobytes()
    assert to_float32(pcm) == pytest.approx([0.0, 32767 / 32768, -1.0])


def test_to_pcm16_clips_instead_of_wrapping() -> None:
    encoded = to_pcm16(np.array([1.5, -1.5], dtype=np.float32))
    assert int16_view(encoded).tolist() == [32767, -32767]


def test_float32_round_trip_stays_within_one_lsb() -> None:
    # 32768 and 32767 cannot both be exact inverses in one scale, so a single
    # least-significant bit of loss is expected and inaudible (-90 dB).
    original = np.array([0, 1, -1, 1000, -1000, 32767, -32768], dtype="<i2")
    recovered = int16_view(to_pcm16(to_float32(original.tobytes())))
    difference = recovered.astype(np.int32) - original.astype(np.int32)
    assert np.max(np.abs(difference)) <= 1


# --- rms_level --------------------------------------------------------------


def test_rms_level_is_zero_for_silence() -> None:
    assert rms_level(b"") == 0.0
    assert rms_level(np.zeros(512, dtype="<i2").tobytes()) == 0.0


def test_rms_level_is_one_for_a_full_scale_square_wave() -> None:
    square = np.tile(np.array([32767, -32768], dtype="<i2"), 256)
    assert rms_level(square.tobytes()) == pytest.approx(1.0, abs=1e-3)


# --- iter_frames ------------------------------------------------------------


def test_iter_frames_yields_exact_frames_and_drops_the_tail() -> None:
    pcm = bytes(range(256)) * 10  # 2560 bytes = 2 full 1024-byte frames + 512
    frames = list(iter_frames(pcm, 1024))
    assert len(frames) == 2
    assert all(len(frame) == 1024 for frame in frames)
    assert b"".join(frames) == pcm[:2048]


def test_iter_frames_yields_nothing_for_an_empty_buffer() -> None:
    assert list(iter_frames(b"", 1024)) == []


def test_iter_frames_rejects_a_non_positive_frame_size() -> None:
    with pytest.raises(ValueError, match="frame_bytes"):
        list(iter_frames(b"\x00\x00", 0))


# --- wrap_wav ---------------------------------------------------------------


def test_wrap_wav_produces_a_header_the_stdlib_can_read_back() -> None:
    pcm = _sine(440.0, 16000, 0.1)
    wrapped = wrap_wav(pcm, 16000)

    with wave.open(io.BytesIO(wrapped), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == SAMPLE_WIDTH_BYTES
        assert wav.getframerate() == 16000
        assert wav.getnframes() == sample_count(pcm)
        # Byte-identical payload proves the samples were never re-encoded.
        assert wav.readframes(wav.getnframes()) == pcm


def test_wrap_wav_writes_the_requested_channel_count() -> None:
    wrapped = wrap_wav(b"\x00\x00\x00\x00", 16000, channels=2)
    with wave.open(io.BytesIO(wrapped), "rb") as wav:
        assert wav.getnchannels() == 2


# --- resample_pcm -----------------------------------------------------------


def test_resample_pcm_returns_the_same_object_for_matching_rates() -> None:
    pcm = _sine(440.0, 24000, 0.05)
    assert resample_pcm(pcm, 24000, 24000) is pcm


def test_resample_pcm_returns_the_same_object_for_an_empty_buffer() -> None:
    assert resample_pcm(b"", 24000, 16000) == b""


def test_resample_pcm_rejects_a_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="sample rates"):
        resample_pcm(b"\x00\x00", 0, 16000)


def test_resample_pcm_keeps_the_duration() -> None:
    pcm = _sine(440.0, 24000, 0.5)
    assert sample_count(resample_pcm(pcm, 24000, 16000)) == pytest.approx(8000, abs=1)
    assert sample_count(resample_pcm(pcm, 24000, 48000)) == pytest.approx(24000, abs=1)


def test_resample_pcm_preserves_an_in_band_tone() -> None:
    pcm = _sine(1000.0, 24000, 0.5, amplitude=0.5)
    downsampled = resample_pcm(pcm, 24000, 16000)

    # Zero crossings are delay-invariant, so they prove the frequency survived
    # without having to align the two signals sample for sample.
    crossings = np.count_nonzero(np.diff(np.signbit(to_float32(downsampled))))
    assert crossings == pytest.approx(2 * 1000 * 0.5, rel=0.05)
    assert rms_level(downsampled) == pytest.approx(rms_level(pcm), rel=0.05)


def test_resample_pcm_rejects_a_tone_above_the_target_nyquist() -> None:
    # 11 kHz cannot exist below an 8 kHz Nyquist. Unfiltered it would fold back
    # to an audible 5 kHz whistle; the anti-alias filter must remove it instead.
    above_nyquist = resample_pcm(_sine(11000.0, 24000, 0.5), 24000, 16000)
    in_band = resample_pcm(_sine(1000.0, 24000, 0.5), 24000, 16000)
    assert rms_level(above_nyquist) < 0.05 * rms_level(in_band)
