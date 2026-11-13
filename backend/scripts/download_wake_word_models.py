"""download_wake_word_models.py — fetch and verify the three openWakeWord graphs.

Why a script instead of `pip install openwakeword`: that package requires
`tflite-runtime` on Linux, and `tflite-runtime` publishes no cp312/cp313 wheels,
so it cannot be resolved on this project's Python. We ship the ONNX graphs in the
repo instead and drive them with the `onnxruntime` we already depend on, exactly
as `app/voice/models/silero_vad.onnx` is shipped.

Why the bytes are pinned here: the download URL is a moving target (GitHub
release assets) and the runtime image builds offline, so a silent change to a
model must fail this script rather than change the assistant's behaviour. Size
AND SHA256 are both checked before anything is written to the package.

Usage (from backend/, no network needed with --check):

    uv run python scripts/download_wake_word_models.py
    uv run python scripts/download_wake_word_models.py --check
    uv run python scripts/download_wake_word_models.py --force

`--check` hashes the committed files and exits non-zero on a mismatch, so CI can
prove a fresh checkout carries the exact graphs the code expects.
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

# The release openWakeWord 0.6.0 itself downloads from; the ONNX files are built
# and published per release, so this tag IS the model version.
RELEASE_TAG = "v0.5.1"
RELEASE_BASE = (
    f"https://github.com/dscripka/openWakeWord/releases/download/{RELEASE_TAG}"
)
RELEASE_API = (
    f"https://api.github.com/repos/dscripka/openWakeWord/releases/tags/{RELEASE_TAG}"
)

# Where the package expects them: alongside this file's siblings at import time.
DEFAULT_DEST = (
    Path(__file__).resolve().parents[1] / "app" / "voice" / "models" / "openwakeword"
)

DOWNLOAD_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class Asset:
    """One pinned model file.

    Attributes:
        name: Filename inside the model directory.
        size: Exact byte count, checked before hashing.
        sha256: Hex digest of the exact bytes this repo expects.
        role: What the graph does, used in the summary output.
    """

    name: str
    size: int
    sha256: str
    role: str


# The three digests below are PUBLISHED MODEL CHECKSUMS, not credentials. They are
# high-entropy hex, so detect-secrets flags them; the inline pragma is how that
# tool documents a reviewed false positive, and it keeps the entropy filter
# enabled for everything else in this file.
ASSETS = (
    Asset(
        "melspectrogram.onnx",
        1_087_958,
        "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",  # pragma: allowlist secret
        "waveform -> 32-bin mel frames",
    ),
    Asset(
        "embedding_model.onnx",
        1_326_578,
        "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",  # pragma: allowlist secret
        "76 mel frames -> 96-float embedding",
    ),
    Asset(
        "hey_jarvis_v0.1.onnx",
        1_271_370,
        "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",  # pragma: allowlist secret
        "16 embeddings -> wake score",
    ),
)


def _digest(data: bytes) -> str:
    """Return the lowercase hex SHA256 of a byte string."""
    return hashlib.sha256(data).hexdigest()


def _read_asset(url: str) -> bytes:
    """Fetch one URL, refusing to return anything that is not a plausible model.

    A failed GitHub request answers with a tiny JSON or HTML error page and a
    200-quality body in some proxy setups; a 107-byte "file" was already written
    by hand once during development. The size floor is a cheap guard; the caller
    still verifies the exact size and hash.

    Args:
        url: Absolute URL to fetch.

    Returns:
        The response body.

    Raises:
        urllib.error.URLError: if the request fails at the transport level.
    """
    request = urllib.request.Request(
        url, headers={"Accept": "application/octet-stream", "User-Agent": "ipa-build"}
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.status != 200:
            raise urllib.error.HTTPError(
                url, response.status, "unexpected status", response.headers, None
            )
        body = response.read()
    if len(body) < 100_000:
        raise ValueError(f"{url} answered with {len(body)} bytes, not a model")
    return body


def _fetch_direct(asset: Asset) -> bytes:
    """Fetch an asset from the release URL, retrying transient gateway errors."""
    url = f"{RELEASE_BASE}/{asset.name}"
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return _read_asset(url)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            last_error = exc
            # Observed in practice: the same asset answers 503/504 on one attempt
            # and 200 on the next. Retrying is cheaper than a manual re-run.
            print(f"  {asset.name}: attempt {attempt} failed ({exc}); retrying")
            time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise RuntimeError(f"could not download {asset.name}") from last_error


def _fetch_via_api(asset: Asset) -> bytes:
    """Fetch an asset through the releases API, the fallback path.

    The CDN URL occasionally refuses a specific object while the API endpoint
    redirects to a working one, so this exists as a real second path rather than
    a theoretical one.

    Args:
        asset: The pinned asset to fetch.

    Returns:
        The response body.

    Raises:
        RuntimeError: if the release metadata or the asset id is unavailable.
    """
    with urllib.request.urlopen(RELEASE_API, timeout=TIMEOUT_SECONDS) as response:
        release = json.load(response)
    asset_id = next(
        (
            item["id"]
            for item in release.get("assets", [])
            if item["name"] == asset.name
        ),
        None,
    )
    if asset_id is None:
        raise RuntimeError(f"{asset.name} is not an asset of {RELEASE_TAG}")
    return _read_asset(
        f"https://api.github.com/repos/dscripka/openWakeWord/releases/assets/{asset_id}"
    )


def _download(asset: Asset) -> bytes:
    """Fetch an asset, falling back to the API endpoint, then verify its bytes.

    Args:
        asset: The pinned asset.

    Returns:
        The verified bytes.

    Raises:
        RuntimeError: on a failed download.
        ValueError: when the size or hash does not match the pin.
    """
    try:
        body = _fetch_direct(asset)
    except RuntimeError:
        print(f"  {asset.name}: release URL exhausted, trying the API asset endpoint")
        body = _fetch_via_api(asset)

    # Size before hash: it fails fast and yields the more useful message when a
    # truncated body slipped through.
    if len(body) != asset.size:
        raise ValueError(f"{asset.name}: expected {asset.size} bytes, got {len(body)}")
    if _digest(body) != asset.sha256:
        raise ValueError(f"{asset.name}: SHA256 does not match the pin")
    return body


def _check(dest: Path) -> int:
    """Verify committed files against the pins without touching the network.

    Args:
        dest: Directory holding the model files.

    Returns:
        0 when every file matches, 1 otherwise.
    """
    failures = 0
    for asset in ASSETS:
        path = dest / asset.name
        if not path.is_file():
            print(f"MISSING {asset.name}")
            failures += 1
            continue
        data = path.read_bytes()
        if len(data) != asset.size or _digest(data) != asset.sha256:
            print(f"CHANGED {asset.name} ({len(data)} bytes, {_digest(data)[:12]}…)")
            failures += 1
            continue
        print(f"OK      {asset.name} ({asset.size} bytes)")
    return 1 if failures else 0


def main() -> int:
    """Download, verify and write the three graphs.

    Returns:
        0 on success, 1 when any asset failed verification.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        help="model directory (default: app/voice/models/openwakeword)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify existing files only; never download",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite a file whose hash does not match",
    )
    args = parser.parse_args()

    if args.check:
        return _check(args.dest)

    args.dest.mkdir(parents=True, exist_ok=True)
    for asset in ASSETS:
        target = args.dest / asset.name
        if target.is_file() and _digest(target.read_bytes()) == asset.sha256:
            print(f"present {asset.name}")
            continue
        if target.is_file() and not args.force:
            print(
                f"refusing to overwrite {asset.name}: hash differs "
                f"(pass --force after checking why)"
            )
            return 1
        print(f"fetch   {asset.name} ({asset.role})")
        data = _download(asset)
        # Write next to the target and rename, so an interrupted run can never
        # leave a half model where the package will load it.
        partial = target.with_suffix(target.suffix + ".part")
        partial.write_bytes(data)
        partial.replace(target)
        print(f"wrote   {asset.name} ({asset.size} bytes)")

    print("\nREADME rows:\n")
    for asset in ASSETS:
        print(f"| `{asset.name}` | {asset.size} | `{asset.sha256}` |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
