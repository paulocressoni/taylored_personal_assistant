"""PCM framing and conversion helpers shared by the voice pipeline.

Every audio buffer on the voice wire is little-endian signed 16-bit mono PCM, so
this module is where that assumption lives exactly once: the byte width, the
scaling between int16 and float32, frame splitting, WAV wrapping and rate
conversion. It is stdlib plus numpy on purpose — the runtime image installs no
apt packages, so there is no ffmpeg (or any other external codec) to fall back
on, and the resampler therefore has to be ours.
"""

import io
import wave
from collections.abc import Iterator

import numpy as np

SAMPLE_WIDTH_BYTES = 2
INT16_FULL_SCALE = 32768.0
INT16_MAX = 32767

# Odd, so the kernel is symmetric about a sample and `mode="same"` adds no
# half-sample shift. 65 taps is far more than the ratios we use require.
_ANTI_ALIAS_TAPS = 65


def int16_view(pcm: bytes) -> np.ndarray:
    """Reinterpret a PCM buffer as int16 samples without copying.

    The dtype is spelled `<i2` rather than `np.int16` because the wire contract
    is s16le: on a big-endian host, `np.int16` would silently decode every
    sample backwards.

    Args:
        pcm: Raw little-endian signed 16-bit samples.

    Returns:
        A read-only int16 view over `pcm`. Read-only because `bytes` is
        immutable, so callers must copy before writing (e.g. with `astype`).
    """
    return np.frombuffer(pcm, dtype="<i2")


def to_float32(pcm: bytes) -> np.ndarray:
    """Convert PCM to float32 samples in `[-1.0, 1.0)`.

    This is the form the VAD model and the resampler expect.

    Args:
        pcm: Raw little-endian signed 16-bit samples.

    Returns:
        A new float32 array, where dividing by the full scale maps 0 to 0.0 and
        -32768 to -1.0.
    """
    return int16_view(pcm).astype(np.float32) / INT16_FULL_SCALE


def to_pcm16(samples: np.ndarray) -> bytes:
    """Convert float32 samples to little-endian signed 16-bit PCM.

    Values outside `[-1.0, 1.0]` are clipped first: `astype` casts by wrapping
    rather than saturating, so a loud passage would come back as noise.

    Args:
        samples: Float samples, nominally in `[-1.0, 1.0]`.

    Returns:
        The encoded buffer, two bytes per sample.
    """
    clipped = np.clip(samples, -1.0, 1.0) * INT16_MAX
    return np.round(clipped).astype("<i2").tobytes()


def sample_count(pcm: bytes) -> int:
    """Return the number of int16 samples in a PCM buffer.

    Args:
        pcm: Raw little-endian signed 16-bit samples.

    Returns:
        The sample count; an odd trailing byte is ignored.
    """
    return len(pcm) // SAMPLE_WIDTH_BYTES


def duration_s(pcm: bytes, sample_rate: int) -> float:
    """Return how many seconds of audio a PCM buffer holds.

    Args:
        pcm: Raw little-endian signed 16-bit samples.
        sample_rate: Samples per second of `pcm`.

    Returns:
        The duration in seconds.

    Raises:
        ValueError: if sample_rate is not positive.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return sample_count(pcm) / sample_rate


def rms_level(pcm: bytes) -> float:
    """Return the loudness of a PCM buffer as an RMS level in `[0.0, 1.0]`.

    Used for logs and for spotting a stream that is delivering pure silence. It
    is NOT an endpointing signal — the VAD stays authoritative.

    Args:
        pcm: Raw little-endian signed 16-bit samples.

    Returns:
        The root-mean-square amplitude, or `0.0` for an empty buffer.
    """
    if not pcm:
        return 0.0
    samples = int16_view(pcm).astype(np.float32) / INT16_FULL_SCALE
    return float(np.sqrt(np.mean(np.square(samples))))


def iter_frames(pcm: bytes, frame_bytes: int) -> Iterator[bytes]:
    """Split a PCM buffer into fixed-size frames.

    A trailing partial frame is DROPPED, never yielded: the VAD needs whole
    512-sample windows, and a truncated frame sent to a client would be played
    as a click. Callers that must not lose the tail keep it themselves.

    Args:
        pcm: Raw little-endian signed 16-bit samples.
        frame_bytes: Size of each frame (1024 for a 512-sample window).

    Yields:
        Consecutive frames of exactly `frame_bytes` bytes.

    Raises:
        ValueError: if frame_bytes is not positive.
    """
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    for start in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        yield pcm[start : start + frame_bytes]


def wrap_wav(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw PCM in a WAV container.

    Uses the stdlib `wave` module, which only prepends a header to the samples:
    nothing is re-encoded and no external tool is invoked.

    Args:
        pcm: Raw little-endian signed 16-bit samples.
        sample_rate: Samples per second of `pcm`.
        channels: Channel count for the header; the pipeline itself is mono.

    Returns:
        A complete WAV file as bytes.
    """
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(SAMPLE_WIDTH_BYTES)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return buffer.getvalue()


def _anti_alias_kernel(from_rate: int, to_rate: int) -> np.ndarray | None:
    """Build a windowed-sinc low-pass kernel for a downsampling ratio.

    Downsampling without a filter folds everything above the new Nyquist back
    into the audible band — 24 kHz to 16 kHz would turn 11 kHz sibilance into a
    5 kHz whistle — so the signal is filtered before it is decimated.
    Upsampling cannot alias and needs no filter.

    Args:
        from_rate: Source sample rate.
        to_rate: Target sample rate.

    Returns:
        A normalised float32 kernel of `_ANTI_ALIAS_TAPS` taps, or `None` when
        the target rate is not lower and no filtering is required.
    """
    if to_rate >= from_rate:
        return None
    cutoff = 0.5 * to_rate / from_rate
    positions = np.arange(_ANTI_ALIAS_TAPS, dtype=np.float64)
    centre = positions[-1] / 2.0
    kernel = 2.0 * cutoff * np.sinc(2.0 * cutoff * (positions - centre))
    kernel *= np.hamming(_ANTI_ALIAS_TAPS)
    return (kernel / kernel.sum()).astype(np.float32)


def resample_pcm(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Convert PCM between sample rates.

    Needed because the TTS provider streams 24 kHz while the reSpeaker XVF3800
    captures and plays at 16 kHz. A browser session asks for 24 kHz, so it never
    pays for this function at all.

    Args:
        pcm: Raw little-endian signed 16-bit samples.
        from_rate: Sample rate of `pcm`.
        to_rate: Sample rate to convert to.

    Returns:
        The resampled buffer, or `pcm` itself when the rates already match or
        the buffer is empty.

    Raises:
        ValueError: if either rate is not positive.
    """
    if from_rate <= 0 or to_rate <= 0:
        raise ValueError("sample rates must be positive")
    if from_rate == to_rate or not pcm:
        return pcm

    samples = to_float32(pcm)
    kernel = _anti_alias_kernel(from_rate, to_rate)
    if kernel is not None and len(samples) >= _ANTI_ALIAS_TAPS:
        samples = np.convolve(samples, kernel, mode="same").astype(np.float32)

    # Output sample i reads the input at i * (from / to), linearly interpolated.
    target_count = round(len(samples) * to_rate / from_rate)
    source_positions = np.clip(
        np.arange(target_count, dtype=np.float64) * (from_rate / to_rate),
        0.0,
        len(samples) - 1.0,
    )
    source_axis = np.arange(len(samples), dtype=np.float64)
    return to_pcm16(np.interp(source_positions, source_axis, samples))
