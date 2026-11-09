"""wake_word_eval.py — score WAV files against the real wake-word detector.

Why this exists: "the wake word feels fine" is not a measurement. The model has a
single threshold, and the only way to choose it is to score real audio and count
the two errors that matter:

  * a **false accept** — room audio that crosses the threshold, which opens a
    turn nobody asked for;
  * a **false reject** — a real attempt that stays below it, which makes the
    assistant ignore you.

Labels come from the filename, so a directory of recordings needs no manifest:

    wake_*          a positive: audio that DOES contain the phrase
    anything else   a negative: speech, TV, music, silence

Run it over the committed fixtures for a sanity check, and over your own room
recordings at 1 m and 4 m for the numbers that actually decide the threshold:

    uv run python scripts/wake_word_eval.py tests/fixtures/*.wav
    uv run python scripts/wake_word_eval.py recordings/1m/*.wav --threshold 0.6

The realtime factor it prints is the CPU answer: how many seconds of audio the
detector scores per second of wall clock (single-threaded).

Usage (from backend/, models already downloaded):
    uv run python scripts/wake_word_eval.py tests/fixtures/*.wav
"""

import argparse
import sys
from pathlib import Path

import numpy as np

from app.core.timing import monotonic_now
from app.voice.audio import duration_s, int16_view, read_wav, resample_pcm
from app.voice.wake import OpenWakeWordDetector, OpenWakeWordModels

# A file is a positive when its name starts with this, which keeps "the audio
# contains the phrase" a property of the recording rather than of a spreadsheet.
POSITIVE_PREFIX = "wake_"


def _resolve(paths: list[Path]) -> list[Path]:
    """Turn the CLI arguments into a list of WAV files, or explain why not.

    A shell only expands a glob when it matches something, so a mistyped or not
    yet created directory arrives here as the literal string `*.wav`. Reporting
    that as "unreadable" hides the real cause, so it is named explicitly. A
    directory argument is expanded here, which is what makes
    `wake_word_eval.py recordings/1m` work without a glob at all.

    Args:
        paths: The arguments as the shell passed them.

    Returns:
        The files to score, in the order given, with directories expanded.

    Raises:
        SystemExit: when nothing matched, so a typo cannot look like a pass.
    """
    resolved: list[Path] = []
    for path in paths:
        if path.is_dir():
            found = sorted(path.glob("*.wav"))
            if not found:
                raise SystemExit(f"{path} contains no .wav files")
            resolved.extend(found)
        elif path.is_file():
            resolved.append(path)
        elif any(character in str(path) for character in "*?["):
            raise SystemExit(
                f"no files matched {path} — the shell only expands a glob when at "
                "least one file exists, so this argument reached the script "
                "literally. Record some clips first, or pass a directory."
            )
        else:
            raise SystemExit(f"{path} does not exist")
    return resolved


def _label(path: Path) -> bool:
    """Return True when the file is a positive sample.

    Args:
        path: The WAV file being scored.

    Returns:
        True for a `wake_*` file, False for every other name.
    """
    return path.name.startswith(POSITIVE_PREFIX)


def _to_mono_16k(pcm: bytes, sample_rate: int, channels: int) -> tuple[bytes, str]:
    """Convert a recording to the detector's contract: mono, 16 kHz, s16le.

    A phone records 48 kHz, often in stereo, and refusing those would make this
    tool useless for the recordings it exists to score. No external decoder is
    involved: both conversions are ones the voice pipeline already performs.

    Args:
        pcm: Raw little-endian signed 16-bit samples, interleaved if stereo.
        sample_rate: The recording's rate.
        channels: 1 or 2.

    Returns:
        The converted PCM and a short note describing what was done.
    """
    note = ""
    if channels > 1:
        samples = int16_view(pcm).reshape(-1, channels)
        mixed = np.clip(np.rint(samples.mean(axis=1)), -32768, 32767)
        pcm = mixed.astype("<i2").tobytes()
        note = f"{channels}ch->mono"
    if sample_rate != 16000:
        pcm = resample_pcm(pcm, sample_rate, 16000)
        note = f"{note} {sample_rate}->16k".strip()
    return pcm, note


def _score(models: OpenWakeWordModels, pcm: bytes, sample_rate: int) -> list[float]:
    """Score one recording and return the per-hop scores.

    Args:
        models: The loaded graphs.
        pcm: Raw 16 kHz mono s16le audio.
        sample_rate: Rate of `pcm`, validated by the caller.

    Returns:
        One score per 80 ms hop that had a full feature history.
    """
    detector = OpenWakeWordDetector(
        models,
        # The threshold is applied by the caller, not here: this tool needs the
        # distribution, and a detection would hide every score below it.
        threshold=1.0,
        debounce_s=0.0,
        seed=models.silence_embedding,
    )
    return detector.push_scores(pcm)


def main() -> int:
    """Score every file, print a table and the two error rates.

    Returns:
        0 when every requested file was scored, 1 when one could not be read.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "files",
        nargs="+",
        type=Path,
        help="WAV files or directories of WAV files to score",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="operating point for the accept/reject counts (default: the setting)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="wake model filename inside app/voice/models/openwakeword/",
    )
    args = parser.parse_args()

    from app.core.config import settings

    threshold = (
        args.threshold
        if args.threshold is not None
        else (settings.voice_wake_word_threshold)
    )
    models = OpenWakeWordModels(args.model) if args.model else OpenWakeWordModels()

    print(f"threshold {threshold:.2f}   model {args.model or 'default'}\n")
    print(f"{'file':<34}{'label':<10}{'peak':>7}{'at':>8}  {'audio':>6}")
    print("-" * 72)

    positives = negatives = 0
    false_rejects = false_accepts = 0
    total_audio = 0.0
    total_wall = 0.0
    failures = 0

    for path in _resolve(args.files):
        try:
            pcm, rate, channels = read_wav(path)
        except (OSError, ValueError) as exc:
            print(f"{path.name:<34}unreadable ({exc})")
            failures += 1
            continue
        # Phone recordings are 48 kHz and frequently stereo; convert rather than
        # skip, because these are exactly the files this tool exists to measure.
        pcm, conversion = _to_mono_16k(pcm, rate, channels)
        rate, channels = 16000, 1

        started = monotonic_now()
        scores = _score(models, pcm, rate)
        elapsed = monotonic_now() - started
        seconds = duration_s(pcm, rate)
        total_audio += seconds
        total_wall += elapsed

        if not scores:
            print(f"{path.name:<34}too short to score ({seconds:.2f}s)")
            failures += 1
            continue

        peak = max(scores)
        peak_at = (scores.index(peak) + 10) * 0.08  # first embedding lands on hop 10
        accepted = peak >= threshold
        positive = _label(path)

        if positive:
            positives += 1
            false_rejects += 0 if accepted else 1
        else:
            negatives += 1
            false_accepts += 1 if accepted else 0

        label = "wake" if positive else "no-wake"
        verdict = "ok" if accepted == positive else "MISS"
        if conversion:
            verdict = f"{verdict} ({conversion})"
        print(
            f"{path.name:<34}{label:<10}{peak:>7.3f}{peak_at:>7.2f}s"
            f"  {seconds:>5.2f}s  {verdict}"
        )

    print("-" * 72)
    if positives:
        print(
            f"false reject  {false_rejects}/{positives}"
            f"  ({false_rejects / positives:.0%})"
        )
    if negatives:
        print(
            f"false accept  {false_accepts}/{negatives}"
            f"  ({false_accepts / negatives:.0%})"
        )
    if total_wall > 0:
        print(f"realtime      {total_audio / total_wall:.1f}x (single-threaded)")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
