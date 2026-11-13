"""make_wake_word_corpus.py — build a labelled test corpus without a microphone.

Why this exists: the most useful number for a wake word is not "how does it score
on one clean clip" but "how much noise can it take before it stops working". That
can be measured without recording anything, by synthesising a few base clips and
deriving every condition from them offline.

The recipe is deliberately "synthesise few, derive many": TTS costs money and is
not deterministic, so a handful of base clips are generated once, then each
degradation is a pure function of the samples and is therefore reproducible.

Derivations (all numpy, no ffmpeg — this host has none):

  snr{N}      white noise mixed in at N dB signal-to-noise ratio
  far         one-pole low-pass plus a level drop: what distance does to speech
  reverb      convolution with an exponentially decaying noise burst
  fast/slow   resampling the clip, which raises or lowers pitch and tempo the way
              a different speaker would

Output layout, one directory per condition, named so `wake_word_eval.py` labels
them without a manifest (`wake_*` is a positive, everything else a negative):

    corpus/snr20/wake_marin.wav
    corpus/snr20/tv_chatter_1.wav
    ...

Then score a condition at a time:

    uv run python scripts/wake_word_eval.py corpus/snr20
    uv run python scripts/wake_word_eval.py corpus/far

⚠️ This is a SYNTHETIC baseline. It says how much margin the detector has against
noise, and it is the right tool for choosing a threshold. It cannot say how the
XVF3800's beam-forming, the automatic gain control, or a real room's reverb
behave — those need recordings from the device, and the numbers here must never
be quoted as real-room recall.

Usage (from backend/, with the voice keys in .env.dev):
    uv run python scripts/make_wake_word_corpus.py --out /tmp/wake-corpus
    uv run python scripts/make_wake_word_corpus.py --out /tmp/wake-corpus --voices marin,cedar
"""

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np

from app.core.config import settings
from app.language.voices import voice_for
from app.voice.audio import int16_view, read_wav, resample_pcm, wrap_wav
from app.voice.registry import build_voice_providers
from app.voice.tts import PCM_SAMPLE_RATE

SAMPLE_RATE = 16000

# The phrase under test. Kept as the bare wake word because that is what recall is
# measured on: a command after it does not make the phrase easier to hear.
POSITIVE = "Hey Jarvis"

# A TV does not say "Hey Jarvis", it says sentences. These stand in for the
# negative class; the committed question fixtures are scored alongside them.
CHATTER = (
    "And that is why the committee decided to postpone the vote until spring.",
    "Next up, the weather for the weekend and a look at the traffic on the ring road.",
    "I told him the keys were in the kitchen, but he never listens to me.",
)

# A small voice set with different pitch and timbre, so the positive class is not
# one synthetic speaker repeated.
DEFAULT_VOICES = ("marin", "cedar", "coral", "alloy", "onyx", "shimmer")

# Noise levels, in dB SNR. 40 is effectively clean, 5 is a loud room.
SNR_LEVELS = (40, 30, 20, 15, 10, 5)


def _rms(samples: np.ndarray) -> float:
    """Return the root-mean-square level of a float signal, floored at 1.0."""
    return float(max(np.sqrt(np.mean(samples**2)), 1.0))


def _add_noise(pcm: bytes, snr_db: float, seed: int) -> bytes:
    """Mix white noise into a clip at a target signal-to-noise ratio.

    Args:
        pcm: Mono 16 kHz s16le audio.
        snr_db: Signal-to-noise ratio to reach, in dB.
        seed: Seed, so a condition is reproducible.

    Returns:
        The mixed audio, clipped to the int16 range.
    """
    signal = int16_view(pcm).astype(np.float64)
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(signal.shape)
    target = _rms(signal) / (10 ** (snr_db / 20))
    noise *= target / _rms(noise)
    mixed = np.clip(signal + noise, -32768, 32767)
    return mixed.astype("<i2").tobytes()


def _low_pass(pcm: bytes, cutoff_hz: float = 2500.0, taps: int = 129) -> bytes:
    """Attenuate the highs, which is most of what distance does to speech.

    A windowed-sinc FIR rather than a one-pole filter: it has a usable stopband,
    and `np.convolve` is the only dependency numpy does not already give us.

    Args:
        pcm: Mono 16 kHz s16le audio.
        cutoff_hz: -6 dB point of the filter.
        taps: Filter length; odd, so `mode="same"` stays centred.

    Returns:
        The filtered audio.
    """
    if taps % 2 == 0:
        taps += 1
    n = np.arange(taps) - (taps - 1) / 2
    kernel = np.sinc(2 * cutoff_hz / SAMPLE_RATE * n) * np.hamming(taps)
    kernel /= kernel.sum()

    signal = int16_view(pcm).astype(np.float64)
    filtered = np.convolve(signal, kernel, mode="same")
    return np.clip(filtered, -32768, 32767).astype("<i2").tobytes()


def _reverb(pcm: bytes, decay_s: float = 0.45, seed: int = 1) -> bytes:
    """Convolve with a synthetic room impulse response.

    Args:
        pcm: Mono 16 kHz s16le audio.
        decay_s: Time for the tail to fall to -60 dB.
        seed: Seed for the noise burst that forms the impulse.

    Returns:
        The reverberated audio, normalised back to the input's peak.
    """
    signal = int16_view(pcm).astype(np.float64)
    length = int(SAMPLE_RATE * decay_s)
    rng = np.random.default_rng(seed)
    impulse = rng.standard_normal(length) * np.exp(-6.9 * np.arange(length) / length)
    impulse[0] = 1.0

    wet = np.convolve(signal, impulse / np.abs(impulse).max(), mode="full")
    peak = np.max(np.abs(wet))
    if peak > 0:
        wet *= min(1.0, np.max(np.abs(signal)) / peak)
    return np.clip(wet, -32768, 32767).astype("<i2").tobytes()


def _resample(pcm: bytes, from_rate: int, to_rate: int) -> bytes:
    """Shift pitch and tempo together by resampling and keeping the header.

    A proper pitch shift needs a phase vocoder; resampling is enough here because
    the goal is "a different voice", not a musical transposition.

    Args:
        pcm: Mono s16le audio at `from_rate`.
        from_rate: The rate the samples are played at.
        to_rate: The rate to reinterpret them at.

    Returns:
        Mono 16 kHz s16le audio.
    """
    return resample_pcm(pcm, from_rate, 16000)


def _write(directory: Path, name: str, pcm: bytes) -> None:
    """Write one clip as a 16 kHz mono WAV."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.wav").write_bytes(wrap_wav(pcm, SAMPLE_RATE))


async def _synthesise(text: str, voice: str) -> bytes:
    """Speak one phrase and return it as 16 kHz mono s16le."""
    providers = build_voice_providers()
    if providers is None:
        raise SystemExit(
            "voice is not configured: set VOICE_ENABLED=true and both provider keys"
        )
    chunks = [chunk async for chunk in providers.synthesizer.synthesize(text, voice)]
    return resample_pcm(
        b"".join(chunks), PCM_SAMPLE_RATE, settings.voice_input_sample_rate
    )


async def main() -> int:
    """Synthesise the base clips and write every derived condition.

    Returns:
        0 when the corpus was written, 1 when TTS failed for every voice.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, required=True, help="directory for the corpus"
    )
    parser.add_argument(
        "--voices",
        default=",".join(DEFAULT_VOICES),
        help="comma-separated TTS voices for the positive class",
    )
    parser.add_argument(
        "--keep-base",
        action="store_true",
        help="also write the clean clips into corpus/clean",
    )
    args = parser.parse_args()

    voices = [voice.strip() for voice in args.voices.split(",") if voice.strip()]
    base: dict[str, bytes] = {}

    for voice in voices:
        try:
            base[f"wake_{voice}"] = await _synthesise(POSITIVE, voice)
        except Exception as exc:  # noqa: BLE001 - a missing voice must not abort
            print(f"voice {voice!r} failed: {type(exc).__name__}: {exc}")
    if not any(name.startswith("wake_") for name in base):
        print("no positive clips were synthesised; is the TTS key valid?")
        return 1

    for index, sentence in enumerate(CHATTER, start=1):
        try:
            base[f"tv_chatter_{index}"] = await _synthesise(sentence, voice_for("en"))
        except Exception as exc:  # noqa: BLE001
            print(f"chatter {index} failed: {type(exc).__name__}: {exc}")

    print(f"\nbase clips: {len(base)} ({len(voices)} voices + {len(CHATTER)} chatter)")

    # The committed question fixtures are the other half of the negative class:
    # real questions, already known to score ~0.
    fixtures = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
    for path in sorted(fixtures.glob("voice_*.wav")):
        pcm, rate, channels = read_wav(path)
        if channels == 1 and rate == SAMPLE_RATE:
            base[f"room_{path.stem}"] = pcm

    if args.keep_base:
        for name, pcm in base.items():
            _write(args.out / "clean", name, pcm)
        print(f"wrote {len(base)} clean clips to {args.out / 'clean'}")

    for snr in SNR_LEVELS:
        directory = args.out / f"snr{snr}"
        for name, pcm in base.items():
            _write(directory, name, _add_noise(pcm, snr, seed=snr))
        print(f"wrote {len(base)} clips to {directory}")

    for name, pcm in base.items():
        # Distance costs both bandwidth and level: -12 dB is roughly 4x the
        # distance of the reference clip on a free-field path.
        quieter = np.clip(
            int16_view(_low_pass(pcm)).astype(np.float64) * 0.25, -32768, 32767
        ).astype("<i2")
        _write(args.out / "far", name, quieter.tobytes())
        _write(args.out / "reverb", name, _reverb(pcm))

    for label, rate in (("fast", 15000), ("slow", 17500)):
        for name, pcm in base.items():
            _write(args.out / label, name, _resample(pcm, SAMPLE_RATE, rate))

    print(
        f"\nconditions: clean, {', '.join(f'snr{n}' for n in SNR_LEVELS)}, far, "
        f"reverb, fast, slow\ncorpus root: {args.out}"
    )
    print(
        "\nScore one condition at a time:\n"
        f"  uv run python scripts/wake_word_eval.py {args.out}/snr20"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
