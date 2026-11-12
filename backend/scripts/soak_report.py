"""soak_report.py — turn a soak's three log files into one comparable report.

Why this exists: a soak answers one question ("did this change make voice
worse?") and the raw material is 20 MB of serial console, 5 MB of container log
and an nginx access log with different clocks, different formats and one line
per second of housekeeping. Reading that by eye is how a regression survives a
"looks fine to me" review, and it is why every number in `docs/voice.md` is
pinned by this tool rather than by memory.

Four rules the implementation enforces, each one paid for by a real mistake:

1. **One window.** The satellite log has no wall clock, so lines are placed with
   a device-clock anchor derived from `ws: connected` / `WebSocket ... [accepted]`
   pairs. `START` is NOT the anchor: the capture may begin with a build, and a
   START-based anchor was 35 minutes wrong on the first soak. An unstable anchor
   (pairs disagreeing by more than `ANCHOR_TOLERANCE_S`) is a hard error.
2. **Fail loud.** A metric with no matching lines prints `n/a (0 matching
   lines)`, never `0`, and its denominator is always shown. (The ad-hoc shell
   one-liners this replaces zeroed every number under a German locale, because
   `sort -n` expects a decimal comma there. Python's `float()` and `format()` are
   locale-independent, so this tool cannot inherit that bug — but it can still
   print "0" for "no data", which is the half that matters.)
3. **Self-describing.** `report.json` carries the window, the anchor and its
   spread, the schema version and every non-secret `voice_*` setting, so a later
   soak can be diffed instead of remembered.
4. **One percentile rule.** Linear interpolation on the (n−1) basis, the same in
   every section and in `--help`, because the alternative reports a different
   p50 on the same data.

Usage (from backend/, after a soak):

    uv run python scripts/soak_report.py --until 2026-11-11T17:45:00Z
    uv run python scripts/soak_report.py --until ... --compare previous/report.json

Exit codes: 0 everything computed, 1 a section had no source lines or the anchor
is unusable. `--compare` exits 1 when two reports are not comparable.
"""

import argparse
import json
import logging
import math
import os
import re
import statistics
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

# How far the implied boot times may disagree before the anchor is rejected.
# Real pairs land within a second; anything larger means the two logs are not
# describing the same events (wrong file, restarted container, drifting clock).
ANCHOR_TOLERANCE_S = 2.0
# How many connect/accept pairs take part in the median.
ANCHOR_PAIRS = 20

DEFAULT_DIR = Path.home() / "wake-soak"

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_UPTIME = re.compile(r"[IWE] \((\d+)\)")
_ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z")
_SESSION_END = re.compile(r"session ended after (\d+) s \(([^)]*)\)")
_SCORE = re.compile(r"wake word detected \(score=([\d.]+)")
_LEAD = re.compile(r"voice turn accepted \(wake_lead=(-?[\d.]+)s\)")
_REJECT = re.compile(r"ignored: no wake word \(speech=([\d.]+)s, since_arm=([^)\"]+)\)")
_TIMING = re.compile(r"(\w+)=([\d.]+)ms")
_REPLY_BYTES = re.compile(r"reply audio: (\d+) bytes")
_LED = re.compile(r"leds: state -> (\d+)")
_ACCEPTED = re.compile(r"WebSocket /ws/voice[^\"]*\"? \[accepted\]")
# The boot banner. `app_init` prints it once per start, so the count is the
# number of boots in the capture — and a second boot resets the uptime clock,
# which is why it has to gate the anchor rather than merely be reported.
_BOOT_BANNER = re.compile(r"app_init: Project name:")
_FIRMWARE = re.compile(r"app_init: App version:\s+(\S+)")
_IDF_VERSION = re.compile(r"app_init: ESP-IDF:\s+(\S+)")

# The device's own byte rate, used to turn reply sizes into seconds.
PLAYOUT_BYTES_PER_S = 32000

# The two reasons a row can read `n/a`. They are different facts and the report
# says which one it means: nothing matched a log that exists, or there was no
# device in this capture at all.
NO_SATELLITE = "no satellite log"
# The two flavours of n/a, kept apart because they mean different things and
# only one of them is a fault.
N_A_NO_LINES = "n/a (0 matching lines)"


class LogError(RuntimeError):
    """A log file is missing or unusable."""


def percentile(values: Sequence[float], p: float) -> float | None:
    """Linear-interpolated percentile on the (n−1) basis.

    Args:
        values: The sample; order does not matter.
        p: Fraction in [0, 1].

    Returns:
        The percentile, or None when `values` is empty. Never 0.0 for an empty
        sample: a caller that cannot tell "no data" from "zero" reports a lie.
    """
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def _strip_ansi(line: str) -> str:
    """Remove `idf_monitor`'s colour escapes and the carriage returns it pads."""
    return _ANSI.sub("", line).replace("\r", "")


def _parse_iso(line: str) -> datetime | None:
    """Read the leading UTC timestamp of a `docker logs -t` line, if it has one."""
    match = _ISO.match(line)
    if match is None:
        return None
    return datetime.fromisoformat(match.group(1) + "Z")


def _read_lines(path: Path) -> list[str]:
    """Read a text log, tolerating the bytes a truncated serial capture leaves.

    Args:
        path: The log file.

    Returns:
        Its lines, ANSI-stripped.

    Raises:
        LogError: if the file does not exist.
    """
    if not path.exists():
        raise LogError(f"missing log file: {path}")
    with path.open("r", errors="replace") as handle:
        return [_strip_ansi(line) for line in handle]


def derive_anchor(satellite: list[str], backend: list[str]) -> tuple[datetime, float]:
    """Recover the device boot time from matched connect/accept pairs.

    A satellite `ws: connected` and a backend `WebSocket /ws/voice [accepted]`
    are the same event seen from both ends, so `accept − uptime` is the boot
    time. The median of the first usable pairs is the anchor.

    Args:
        satellite: The serial log's lines.
        backend: The container log's lines.

    Returns:
        The boot time and the spread between the implied boots, in seconds.

    Raises:
        LogError: when no pair can be formed, or when the pairs disagree by more
            than `ANCHOR_TOLERANCE_S` — placing hours of events on a guess is
            worse than refusing to report.
    """
    uptimes: list[int] = []
    for line in satellite:
        if "ws: connected" not in line:
            continue
        match = _UPTIME.search(line)
        if match is not None:
            uptimes.append(int(match.group(1)))

    accepts: list[datetime] = []
    for line in backend:
        if "/ws/voice" not in line or "[accepted]" not in line:
            continue
        stamp = _parse_iso(line)
        if stamp is not None:
            accepts.append(stamp)

    # Pair from the END: the newest events are the least likely to be clipped by
    # the capture boundary or by a log that starts mid-session.
    pairs = min(len(uptimes), len(accepts), ANCHOR_PAIRS)
    if pairs == 0:
        raise LogError(
            "cannot anchor the device clock: no ws: connected line matched a "
            "WebSocket [accepted] line"
        )
    implied = [
        accepts[-offset] - timedelta(milliseconds=uptimes[-offset])
        for offset in range(1, pairs + 1)
    ]
    spread = (max(implied) - min(implied)).total_seconds()
    if spread > ANCHOR_TOLERANCE_S:
        raise LogError(
            f"anchor unstable: {pairs} pairs disagree by {spread:.1f}s "
            f"(limit {ANCHOR_TOLERANCE_S:.0f}s) — misaligned logs or a drifting "
            "device clock"
        )
    return median_time(implied), spread


def median_time(values: list[datetime]) -> datetime:
    """Return the median of a list of timestamps.

    Args:
        values: Non-empty list of timezone-aware datetimes.

    Returns:
        The middle value when sorted (the lower middle for an even count).
    """
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def count_boots(satellite: Sequence[str]) -> int:
    """Count the boots in a serial capture.

    Args:
        satellite: The serial log's lines.

    Returns:
        How many times the application started. Zero when the capture never
        caught a boot banner, which is not an error — only more than one is.
    """
    return sum(1 for line in satellite if _BOOT_BANNER.search(line))


def firmware_version(satellite: Sequence[str]) -> str | None:
    """Read the application version out of the last boot banner.

    Args:
        satellite: The serial log's lines.

    Returns:
        The version string, or None when the banner was not captured.
    """
    for line in reversed(satellite):
        match = _FIRMWARE.search(line)
        if match is not None:
            return match.group(1)
    return None


def idf_version(satellite: Sequence[str]) -> str | None:
    """Read the ESP-IDF version out of the last boot banner.

    Args:
        satellite: The serial log's lines.

    Returns:
        The version string, or None when the banner was not captured.
    """
    for line in reversed(satellite):
        match = _IDF_VERSION.search(line)
        if match is not None:
            return match.group(1)
    return None


def final_boot_segment(satellite: Sequence[str]) -> tuple[list[str], int]:
    """Cut a capture at its last boot, keeping only what came after it.

    A boot resets the uptime clock, so lines from an earlier boot cannot be
    placed on the timeline by adding them to the last boot's epoch: they would
    land hours early and be counted anyway. Dropping them keeps every device
    metric describing one epoch.

    Args:
        satellite: The serial log's lines.

    Returns:
        The lines after the last boot banner, and how many were dropped.
    """
    last = -1
    for index, line in enumerate(satellite):
        if _BOOT_BANNER.search(line):
            last = index
    if last < 0:
        return list(satellite), 0
    return list(satellite[last:]), last + 1


class Section:
    """Accumulates one report section's numbers and its denominators."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.rows: list[tuple[str, str, str]] = []

    @property
    def empty(self) -> bool:
        """True when the section wanted a number and could not find one.

        Two rules, both learned from using the tool on a real capture:

        - a gate that saw 9 rejections and 0 detections has **data**; one n/a
          row must not condemn the section;
        - a server-only soak has device rows that are absent *by design*, and
          failing that report would blame the operator for their own choice.

        So a section is empty only when every row is the "nothing matched"
        flavour — the one that means a log was there and did not answer.
        """
        values = [value for _label, value, _denominator in self.rows]
        if any(not value.startswith("n/a") for value in values):
            return False
        return any(value == N_A_NO_LINES for value in values)

    def add(self, label: str, value: Any, denominator: str = "") -> None:
        """Record one line of the report.

        Args:
            label: What is being reported.
            value: The number, or `None`/"" when it could not be computed.
            denominator: What the number is out of, printed after it.
        """
        text = N_A_NO_LINES if value is None else str(value)
        self.rows.append((label, text, denominator))

    def absent(self, label: str, reason: str, denominator: str = "") -> None:
        """Record a line that cannot exist in this capture, by design.

        Deliberately contributes no data, but does not make the section empty by
        itself: a server-only soak has no device rows because the operator chose
        that, and blaming it on missing data would make the exit code lie.

        Args:
            label: What is being reported.
            reason: Why it is absent, in the operator's words.
            denominator: What the number would have been out of.
        """
        self.rows.append((label, f"n/a ({reason})", denominator))

    def note(self, text: str) -> None:
        """Record a line that is not a number (context, or a warning)."""
        self.rows.append(("", text, ""))

    def render(self) -> str:
        """Format the section as a padded text table."""
        width = max((len(label) for label, _, _ in self.rows), default=0)
        out = [self.name]
        for label, value, denominator in self.rows:
            if not label:
                out.append(f"  {value}")
                continue
            suffix = f"  [{denominator}]" if denominator else ""
            out.append(f"  {label:<{width}}  {value}{suffix}")
        return "\n".join(out)


def build_sessions(
    satellite: list[str] | None,
    backend: list[str],
    boot: datetime,
    until: datetime,
) -> tuple[Section, dict[str, Any]]:
    """Session lifetime, reconnect rate and the signature of each loss.

    Args:
        satellite: The serial log's lines, or None when the capture had no device.
        backend: The container log's lines.
        boot: Start of the reporting window.
        until: Exclusive end of the reporting window.

    Returns:
        The rendered section and the numbers behind it.
    """
    section = Section("Sessions")
    durations: list[int] = []
    poll_write = failed_frames = close_frames = 0
    losses: list[datetime] = []
    for line in satellite or []:
        match = _UPTIME.search(line)
        if match is None:
            continue
        when = boot + timedelta(milliseconds=int(match.group(1)))
        if when >= until:
            continue
        if "transport_poll_write" in line:
            poll_write += 1
        if "failed=1" in line:
            failed_frames += 1
        if "close frame" in line:
            close_frames += 1
        end = _SESSION_END.search(line)
        if end is not None:
            durations.append(int(end.group(1)))
            losses.append(when)

    opens = sum(
        1
        for line in backend
        if '"voice session open' in line and _in_window(line, until, boot)
    )
    closes = sum(
        1
        for line in backend
        if '"voice session closed' in line and _in_window(line, until, boot)
    )

    if satellite is None:
        section.absent("device sessions ended", NO_SATELLITE)
    else:
        section.add("device sessions ended", len(durations) or None)
    section.add("server sessions opened", opens or None, "same window")
    section.add("server sessions closed", closes or None, "same window")
    if satellite is None:
        for label in ("loss signature", "mean", "p50", "p90", "max"):
            section.absent(label, NO_SATELLITE)
        section.absent("no close frame received", NO_SATELLITE, "device losses")
        section.absent("uplink frames failed", NO_SATELLITE, "device losses")
    else:
        section.add(
            "loss signature",
            f"{poll_write} transport_poll_write",
            f"{len(durations)} losses",
        )
        section.add("no close frame received", close_frames, "device losses")
        section.add("uplink frames failed", failed_frames, "device losses")
        section.add(
            "mean",
            f"{statistics.mean(durations):.0f} s" if durations else None,
            f"n={len(durations)}",
        )
        section.add("p50", _fmt_s(percentile(durations, 0.5)), f"n={len(durations)}")
        section.add("p90", _fmt_s(percentile(durations, 0.9)), f"n={len(durations)}")
        section.add(
            "max", f"{max(durations)} s" if durations else None, f"n={len(durations)}"
        )
    if durations:
        buckets = Counter(_duration_bucket(value) for value in durations)
        section.add(
            "distribution",
            ", ".join(f"{count} {name}" for name, count in _ordered_buckets(buckets)),
            f"n={len(durations)}",
        )
        long_sessions = [value for value in durations if value >= 1790]
        if long_sessions:
            section.add(
                ">=1790 s sessions",
                f"{len(long_sessions)} ({Counter(long_sessions).most_common(1)[0][1]} at "
                f"{Counter(long_sessions).most_common(1)[0][0]} s)",
                f"n={len(durations)}",
            )
    if losses:
        span_h = max((losses[-1] - losses[0]).total_seconds() / 3600.0, 0.001)
        section.add(
            "reconnect rate", f"{len(losses) / span_h:.1f}/h", f"{span_h:.1f} h"
        )
        section.add("busiest hour", _busiest_hour(losses))
        section.add("reconciliation", f"{closes} closes vs {len(durations)} ends")
    data = {
        "durations": durations,
        "opens": opens,
        "closes": closes,
        "poll_write": poll_write,
    }
    return section, data


def _in_window(line: str, until: datetime, boot: datetime) -> bool:
    """True when a backend line falls inside `[boot, until)`."""
    stamp = _parse_iso(line)
    return stamp is not None and boot <= stamp < until


def _fmt_s(value: float | None) -> str | None:
    """Format a seconds figure, or None when there was no sample."""
    return None if value is None else f"{value:.0f} s"


def _duration_bucket(seconds: int) -> str:
    """Name the lifetime bucket a session falls in."""
    if seconds < 10:
        return "<10 s"
    if seconds < 30:
        return "10-30 s"
    if seconds < 60:
        return "30-60 s"
    if seconds < 300:
        return "1-5 min"
    if seconds < 900:
        return "5-15 min"
    if seconds < 1790:
        return "15-30 min"
    return ">=1790 s"


def _ordered_buckets(counts: Counter[str]) -> list[tuple[str, int]]:
    """Return duration buckets in ascending order rather than insertion order."""
    order = [
        "<10 s",
        "10-30 s",
        "30-60 s",
        "1-5 min",
        "5-15 min",
        "15-30 min",
        ">=1790 s",
    ]
    return [(name, counts[name]) for name in order if counts[name]]


def _busiest_hour(losses: list[datetime]) -> str:
    """Name the wall-clock hour with the most session losses."""
    hours = Counter(stamp.strftime("%m-%d %H") for stamp in losses)
    hour, count = hours.most_common(1)[0]
    return f"{hour}:00 UTC with {count}"


def build_gate(
    backend: list[str], boot: datetime, until: datetime, threshold: float
) -> tuple[Section, dict[str, Any]]:
    """Detections, accepted turns and why segments were rejected."""
    section = Section("Wake word gate")
    scores: list[float] = []
    leads: list[float] = []
    buckets: Counter[str] = Counter()
    rejected_seconds = 0.0
    accepted = rejected = 0
    for line in backend:
        if not _in_window(line, until, boot):
            continue
        if (score := _SCORE.search(line)) is not None:
            scores.append(float(score.group(1)))
        elif (lead := _LEAD.search(line)) is not None:
            accepted += 1
            leads.append(float(lead.group(1)))
        elif (reject := _REJECT.search(line)) is not None:
            rejected += 1
            rejected_seconds += float(reject.group(1))
            buckets[_arm_bucket(reject.group(2))] += 1

    section.add("detections", len(scores) or None)
    section.add(
        "score p50",
        f"{percentile(scores, 0.5):.2f}" if scores else None,
        f"n={len(scores)}",
    )
    section.add(
        "score p90",
        f"{percentile(scores, 0.9):.2f}" if scores else None,
        f"n={len(scores)}",
    )
    section.add(
        "score below 0.60",
        sum(1 for item in scores if item < 0.6) if scores else None,
        f"n={len(scores)}",
    )
    section.add("accepted turns", accepted or None, f"threshold {threshold:.2f}")
    if leads:
        one_breath = sum(1 for item in leads if item < 0)
        section.add(
            "one-breath turns",
            f"{one_breath} ({one_breath / len(leads):.0%})",
            f"n={len(leads)}",
        )
        section.add(
            "wake_lead p50", f"{percentile(leads, 0.5):.2f} s", f"n={len(leads)}"
        )
    section.add("rejections", rejected or None)
    if rejected:
        for name in ("never armed", ">60 s", "10-60 s", "0.5-6 s", "6-10 s", "<=0.5 s"):
            if buckets[name]:
                section.add(f"  {name}", buckets[name], f"of {rejected}")
        section.add(
            "rejected audio",
            f"{_fmt_audio(rejected_seconds)} never transcribed",
            f"of {accepted or 'n/a'} turns",
        )
    data = {"detections": len(scores), "accepted": accepted, "rejected": rejected}
    return section, data


def _fmt_audio(seconds: float) -> str:
    """Format an audio total with the unit that keeps it readable."""
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min ({seconds / 3600:.1f} h)"


def _arm_bucket(raw: str) -> str:
    """Bucket a rejection by how long ago the permit was armed."""
    if raw.strip() == "never":
        return "never armed"
    try:
        value = float(raw.strip().rstrip("s"))
    except ValueError:
        return "unknown"
    if value <= 0.5:
        return "<=0.5 s"
    if value <= 6:
        return "0.5-6 s"
    if value <= 10:
        return "6-10 s"
    if value <= 60:
        return "10-60 s"
    return ">60 s"


def build_latency(backend: list[str], boot: datetime, until: datetime) -> Section:
    """Stage timings for the turns that reached the graph."""
    section = Section("Latency")
    stages: dict[str, list[float]] = {}
    turns = 0
    for line in backend:
        if "voice turn timing" not in line or not _in_window(line, until, boot):
            continue
        turns += 1
        for name, value in _TIMING.findall(line):
            stages.setdefault(name, []).append(float(value))
    section.add("turns with timings", turns or None)
    for name in ("stt_ms", "graph_ttft_ms", "tts_ttfb_ms", "first_audio_ms", "turn_ms"):
        values = stages.get(name, [])
        if not values:
            section.add(name, None)
            continue
        section.add(
            name,
            f"p50 {percentile(values, 0.5):.0f} · p90 {percentile(values, 0.9):.0f} · "
            f"max {max(values):.0f} ms",
            f"n={len(values)}",
        )
    first_audio = stages.get("first_audio_ms", [])
    if first_audio:
        slow = sum(1 for value in first_audio if value > 3000)
        section.add(
            "first audio > 3 s",
            f"{slow} ({slow / len(first_audio):.0%})",
            f"n={len(first_audio)}",
        )
    return section


def build_playback(
    satellite: list[str] | None, boot: datetime, until: datetime
) -> Section:
    """Reply size and the two ways playout loses audio.

    Args:
        satellite: The serial log's lines, or None when the capture had no device.
        boot: Start of the reporting window.
        until: Exclusive end of the reporting window.

    Returns:
        The rendered section.
    """
    section = Section("Playback")
    if satellite is None:
        for label in (
            "replies with audio",
            "size p50",
            "size p90",
            "size max",
            "ring full (truncated)",
            "underruns",
        ):
            section.absent(label, NO_SATELLITE)
        return section
    sizes: list[int] = []
    ring_full = underruns = 0
    for line in satellite:
        match = _UPTIME.search(line)
        if match is None:
            continue
        if boot + timedelta(milliseconds=int(match.group(1))) >= until:
            continue
        if (reply := _REPLY_BYTES.search(line)) is not None:
            sizes.append(int(reply.group(1)))
        if "ring full" in line:
            ring_full += 1
        if "playout ran dry" in line:
            underruns += 1
    section.add("replies with audio", len(sizes) or None)
    if sizes:
        as_s = lambda value: value / PLAYOUT_BYTES_PER_S
        section.add(
            "size p50",
            f"{percentile(sizes, 0.5):.0f} B ({as_s(percentile(sizes, 0.5)):.1f} s)",
            f"n={len(sizes)}",
        )
        section.add(
            "size p90",
            f"{percentile(sizes, 0.9):.0f} B ({as_s(percentile(sizes, 0.9)):.1f} s)",
            f"n={len(sizes)}",
        )
        section.add(
            "size max",
            f"{max(sizes):.0f} B ({as_s(max(sizes)):.1f} s)",
            f"n={len(sizes)}",
        )
        section.add(
            "ring full (truncated)",
            f"{ring_full} ({ring_full / len(sizes):.0%})",
            f"n={len(sizes)}",
        )
        section.add("underruns", underruns, f"n={len(sizes)}")
    else:
        section.add("ring full (truncated)", None)
        section.add("underruns", None)
    return section


def build_device(
    satellite: list[str] | None,
    boot: datetime,
    until: datetime,
    boots: int,
) -> Section:
    """LED transitions, transcript frames and how often the device restarted.

    Args:
        satellite: The serial log's lines, or None when the capture had no device.
        boot: Start of the reporting window.
        until: Exclusive end of the reporting window.
        boots: How many boot banners the capture contained.

    Returns:
        The rendered section.
    """
    section = Section("Device")
    if satellite is None:
        section.absent("LED transitions", NO_SATELLITE)
        section.absent("transcript frames", NO_SATELLITE)
        section.absent("boots", NO_SATELLITE)
        return section
    leds: Counter[str] = Counter()
    heard = 0
    for line in satellite:
        match = _UPTIME.search(line)
        if match is None:
            continue
        if boot + timedelta(milliseconds=int(match.group(1))) >= until:
            continue
        if (led := _LED.search(line)) is not None:
            leds[led.group(1)] += 1
        if "heard:" in line:
            heard += 1
    names = {"1": "IDLE", "2": "LISTENING", "3": "THINKING", "4": "SPEAKING"}
    section.add(
        "LED transitions",
        ", ".join(f"{leds[key]} {name}" for key, name in names.items() if leds[key])
        or None,
    )
    section.add("transcript frames", heard or None)
    section.add("boots", boots)
    return section


def build_errors(backend: list[str], boot: datetime, until: datetime) -> Section:
    """The server-side failures a soak is long enough to catch."""
    section = Section("Server errors")
    asgi = sum(
        1
        for line in backend
        if "Exception in ASGI application" in line and _in_window(line, until, boot)
    )
    failed = sum(
        1
        for line in backend
        if "voice turn failed" in line and _in_window(line, until, boot)
    )
    timeouts = sum(
        1
        for line in backend
        if "voice turn timed out" in line and _in_window(line, until, boot)
    )
    section.add(
        "ASGI exceptions",
        f"{asgi} (one per vanished client before the close-path fix)",
        "closed sessions",
    )
    section.add("turn failures", failed, "turns")
    section.add("turn timeouts", timeouts, "turns")
    return section


def _span_hours(start: Any, end: Any) -> float | None:
    """Measure a report window in hours.

    Args:
        start: The `window_start` ISO timestamp, or None.
        end: The `until` ISO timestamp, or None.

    Returns:
        The span in hours, or None when either bound is missing or unparseable.
    """
    try:
        return (
            datetime.fromisoformat(end) - datetime.fromisoformat(start)
        ).total_seconds() / 3600
    except (TypeError, ValueError):
        return None


def _fmt_hours(hours: float | None) -> str:
    """Render a span for the comparison header."""
    return f"{hours:.1f} h" if hours is not None else "unknown span"


def _jsonable(value: Any) -> Any:
    """Coerce a settings value into something `json.dumps` can write.

    Args:
        value: Any value out of pydantic's `model_dump()`.

    Returns:
        A JSON scalar, or a string form for anything richer. `SecretStr` masks
        itself as asterisks, so a secret can be named but never printed.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


_SECRET_HINTS = ("key", "secret", "token", "password")


def _meta_time(directory: Path, key: str) -> datetime | None:
    """Read one UTC timestamp the collector recorded into `META`.

    Both bounds of the window come from here: the collector knows when the
    first byte arrived and when the last writer stopped, and a hand-typed bound
    is the `START` mistake in a new hat.

    Args:
        directory: Where `META` lives.
        key: The field name, e.g. `capture_started_utc`.

    Returns:
        The timestamp, or None when META or the key is absent or unparseable.
    """
    path = directory / "META"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, _, value = line.partition("=")
        if name.strip() != key or not value.strip():
            continue
        try:
            stamp = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)
    return None


def collect_meta(until: datetime) -> dict[str, Any]:
    """Describe the code and configuration the capture ran against.

    Args:
        until: The exclusive end of the reporting window.

    Returns:
        A JSON-serialisable record. Secret-bearing settings are excluded by name.
    """
    meta: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "until": until.isoformat(),
    }
    try:
        os.environ.setdefault("ENV", "dev")
        from app._version import __version__
        from app.core.config import settings

        meta["app_version"] = __version__
        meta["voice_settings"] = {
            name: _jsonable(value)
            for name, value in settings.model_dump().items()
            if name.startswith("voice_")
            and not any(hint in name for hint in _SECRET_HINTS)
        }
    except Exception as exc:  # noqa: BLE001 - metadata must never break a report
        meta["settings_error"] = f"{type(exc).__name__}: {exc}"
    try:
        import subprocess

        meta["git_sha"] = (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            or None
        )
        meta["git_dirty"] = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        )
    except Exception as exc:  # noqa: BLE001
        meta["git_error"] = f"{type(exc).__name__}: {exc}"
    return meta


def _fmt_meta(meta: dict[str, Any]) -> str:
    """Render the meta block as a short key/value list."""
    lines = ["Context"]
    for key in ("app_version", "git_sha", "git_dirty", "until"):
        if key in meta:
            lines.append(f"  {key:<14} {meta[key]}")
    settings = meta.get("voice_settings") or {}
    for name in sorted(settings):
        lines.append(f"  {name:<14} {settings[name]}")
    return "\n".join(lines)


def report(
    directory: Path,
    until: datetime,
    since: datetime | None = None,
    allow_reboot: bool = False,
) -> tuple[str, dict[str, Any]]:
    """Build the whole report for one directory of logs.

    Args:
        directory: Where `satellite-soak.log`, `backend.log` and `frontend.log`
            live. The serial log is optional; the other two are not.
        until: Exclusive end of the window.
        since: Start of the window when there is no serial log to anchor to.
            Ignored when one is present, and required when it is not.
        allow_reboot: Report only the segment after the last boot when the
            capture contains more than one. Without it, a reboot is refused.

    Returns:
        The rendered text and the JSON-serialisable report.

    Raises:
        LogError: if a required log is missing, if a server-only capture was
            requested without `since`, or if the device clock cannot be anchored
            — which includes a capture that contains more than one boot.
    """
    satellite_path = directory / "satellite-soak.log"
    satellite = _read_lines(satellite_path) if satellite_path.exists() else None
    try:
        backend = _read_lines(directory / "backend.log")
    except LogError as exc:
        # The soak directory holds one directory per run, so pointing at the
        # parent is the easy mistake. Name the run the operator probably meant.
        runs = (
            sorted(child.name for child in directory.iterdir() if child.is_dir())
            if directory.is_dir()
            else []
        )
        if not runs:
            raise
        raise LogError(
            f"{exc} — {directory} holds {len(runs)} run directories; "
            f"point --dir at one, e.g. --dir {directory / runs[-1]}"
        ) from None

    boots = count_boots(satellite or [])
    dropped = 0
    if satellite is not None and boots > 1:
        if not allow_reboot:
            raise LogError(
                f"device rebooted mid-soak ({boots} boots) — the uptime clock "
                "restarts at each boot, so one window cannot describe the whole "
                "capture. Re-run the soak, or pass --allow-reboot to report the "
                "final segment only"
            )
        satellite, dropped = final_boot_segment(satellite)

    if satellite is None:
        if since is None:
            raise LogError(
                "no satellite-soak.log, so the window cannot be anchored from "
                "connect/accept pairs. Pass --since <ISO-8601 UTC>, or write "
                "capture_started_utc into META with the collector"
            )
        boot, spread, anchor_source = since, None, "since"
    else:
        boot, spread = derive_anchor(satellite, backend)
        anchor_source = "pairs"

    meta = collect_meta(until)
    threshold = float(
        (meta.get("voice_settings") or {}).get("voice_wake_word_threshold", 0.5)
    )
    sessions, session_data = build_sessions(satellite, backend, boot, until)
    gate, gate_data = build_gate(backend, boot, until, threshold)
    sections = [
        sessions,
        gate,
        build_latency(backend, boot, until),
        build_playback(satellite, boot, until),
        build_device(satellite, boot, until, boots),
        build_errors(backend, boot, until),
    ]

    meta.update(
        {
            "window_start": boot.isoformat(),
            "anchor_spread_s": round(spread, 3) if spread is not None else None,
            "anchor_source": anchor_source,
            "satellite_log": satellite is not None,
            "boots": boots,
            "firmware_version": firmware_version(satellite or []),
            "idf_version": idf_version(satellite or []),
            "percentile_rule": "linear, (n-1) basis",
        }
    )
    if anchor_source == "since":
        anchor_line = "anchor: none — no serial log, window starts at the --since bound"
    else:
        anchor_line = (
            f"anchor: device boot {boot.isoformat()} (spread {spread:.2f} s over "
            "connect/accept pairs)"
        )
    header = (
        f"Soak report — window [window_start, {until.isoformat()}) = "
        f"[{boot.isoformat()}, {until.isoformat()})  ·  schema v{SCHEMA_VERSION}\n"
        f"{anchor_line}"
    )
    text = "\n\n".join(
        [header, _fmt_meta(meta)] + [section.render() for section in sections]
    )
    if dropped:
        text = (
            f"NOTE: device rebooted {boots} times; reporting the final boot "
            f"segment only ({dropped} lines before the last boot ignored)\n\n" + text
        )
    payload = {
        "meta": meta,
        "sections": {section.name: section.rows for section in sections},
        "empty_sections": [section.name for section in sections if section.empty],
        "numbers": {
            "sessions": {
                key: value for key, value in session_data.items() if key != "durations"
            },
            "gate": gate_data,
        },
    }
    return text, payload


def compare(
    current: dict[str, Any],
    previous: dict[str, Any],
    allow_firmware_change: bool = False,
) -> tuple[int, str]:
    """Diff two reports, refusing to compare incomparable ones.

    Args:
        current: The report just built.
        previous: A `report.json` from an earlier soak.
        allow_firmware_change: Compare anyway when the two captures ran
            different firmware. Off by default: a firmware bump is a bigger
            variable than any setting, and a delta across it is not a delta.

    Returns:
        An exit code (0 comparable, 1 refused) and the text to print.
    """
    cur_meta = current.get("meta", {})
    prev_meta = previous.get("meta", {})
    problems = []
    # The two windows are NOT part of the refusal set: two captures start at
    # different boots and end at different `--until` bounds by construction, so
    # refusing on them would make the one comparison this tool exists for —
    # "did this change make voice worse than the last soak?" — impossible. The
    # measurement *configuration* has to match; the exposure is reported below.
    for key in ("schema_version", "anchor_source"):
        if cur_meta.get(key) != prev_meta.get(key):
            problems.append(f"{key}: {prev_meta.get(key)!r} -> {cur_meta.get(key)!r}")
    if not allow_firmware_change:
        cur_firmware = cur_meta.get("firmware_version")
        prev_firmware = prev_meta.get("firmware_version")
        if cur_firmware and prev_firmware and cur_firmware != prev_firmware:
            problems.append(
                f"firmware_version: {prev_firmware!r} -> {cur_firmware!r} "
                "(pass --allow-firmware-change to compare anyway)"
            )
    cur_settings = cur_meta.get("voice_settings") or {}
    prev_settings = prev_meta.get("voice_settings") or {}
    for name in ("voice_wake_word_threshold", "voice_wake_word_window_seconds"):
        if cur_settings.get(name) != prev_settings.get(name):
            problems.append(
                f"{name}: {prev_settings.get(name)!r} -> {cur_settings.get(name)!r}"
            )
    if problems:
        return 1, "NOT COMPARABLE — the two captures differ in:\n  " + "\n  ".join(
            problems
        )

    def flatten(report: dict[str, Any]) -> dict[str, str]:
        flat: dict[str, str] = {}
        for section, rows in (report.get("sections") or {}).items():
            for label, value, _denominator in rows:
                if label:
                    flat[f"{section}/{label}"] = value
        return flat

    now, before = flatten(current), flatten(previous)
    prev_h = _span_hours(prev_meta.get("window_start"), prev_meta.get("until"))
    cur_h = _span_hours(cur_meta.get("window_start"), cur_meta.get("until"))
    lines = ["Comparison (previous -> current)"]
    lines.append(
        f"window  : previous {_fmt_hours(prev_h)}  |  current {_fmt_hours(cur_h)}"
    )
    if prev_h and cur_h and abs(cur_h - prev_h) > 0.1 * max(prev_h, cur_h):
        lines.append(
            "note    : the captures cover different spans — the counts below are "
            "totals, not rates"
        )
    for key in sorted(set(now) | set(before)):
        old, new = before.get(key, "—"), now.get(key, "—")
        marker = "  " if old == new else "* "
        lines.append(f"{marker}{key:<42} {old}  ->  {new}")
    return 0, "\n".join(lines)


def main() -> int:
    """Parse the CLI, print the report and write it as JSON.

    Returns:
        0 when every section produced numbers, 1 when one did not (or the two
        reports are not comparable), so a silent zero can never pass CI.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"where the logs live (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--until",
        default=None,
        help=(
            "exclusive end of the window, ISO-8601 UTC (e.g. 2026-11-11T17:45:00Z); "
            "defaults to capture_ended_utc in META"
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        help=(
            "start of the window when there is no satellite-soak.log; defaults "
            "to capture_started_utc in META"
        ),
    )
    parser.add_argument(
        "--allow-reboot",
        action="store_true",
        help="report only the final boot segment when the device restarted",
    )
    parser.add_argument(
        "--allow-firmware-change",
        action="store_true",
        help="compare two captures that ran different firmware",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="where to write report.json (default: <dir>/report.json)",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        help="an earlier report.json to diff against",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    if args.until:
        until = datetime.fromisoformat(args.until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=UTC)
    else:
        until = _meta_time(args.dir, "capture_ended_utc")
        if until is None:
            print(
                "soak_report: no --until given and no capture_ended_utc in META. "
                "Pass --until <ISO-8601 UTC>; it is the exclusive end of the "
                "window, so it should be the moment the capture stopped",
                file=sys.stderr,
            )
            return 1
        print(
            f"until: {until.isoformat()} (from META capture_ended_utc)", file=sys.stderr
        )
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
    elif not (args.dir / "satellite-soak.log").exists():
        # Only worth resolving when it will be used: a serial log anchors the
        # window itself, and announcing a bound that is then ignored is noise.
        since = _meta_time(args.dir, "capture_started_utc")
        if since is not None:
            print(
                f"since: {since.isoformat()} (from META capture_started_utc)",
                file=sys.stderr,
            )

    try:
        text, payload = report(
            args.dir, until, since=since, allow_reboot=args.allow_reboot
        )
    except LogError as exc:
        print(f"soak_report: {exc}", file=sys.stderr)
        return 1

    print(text)
    out = args.out or (args.dir / "report.json")
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"\nwritten  : {out}")

    code = 0
    empty: list[str] = payload.get("empty_sections") or []
    if empty:
        print(f"WARNING  : no source lines for {', '.join(empty)}", file=sys.stderr)
        code = 1

    if args.compare is not None:
        previous = json.loads(args.compare.read_text(encoding="utf-8"))
        compare_code, text_diff = compare(
            payload, previous, allow_firmware_change=args.allow_firmware_change
        )
        print("\n" + text_diff)
        code = max(code, compare_code)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
