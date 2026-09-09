#!/usr/bin/env python3
"""Scheduled, encrypted backups for the prod stack on the DeskMini (M28).

Runs as a root cron job and produces one dated backup directory per run under
`BACKUP_ROOT/daily/YYYY-MM-DD/`. Every item is written to a staging file,
hashed, then age-encrypted (`.age`) before the plaintext is removed. A
`manifest.txt` records the SHA-256 of each item's DECRYPTED contents, so a
verify/restore drill can prove integrity without keeping the age identity on
the backup box.

What is backed up (each item is a config switch, so anything not yet in the
stack is simply left off):

* `app-postgres`      - pg_dump (custom format) of the M28 checkpointer DB on
                        the `app-db` service. This holds the assistant's
                        conversation memory AND the future `scheduled_items`
                        table, so it is the most critical item.
* `langfuse-postgres` - pg_dump of Langfuse's own Postgres (org/project/user
                        records). Losing it means rebuilding the M08 project
                        and re-matching API keys by hand.
* `VOLUMES`           - file-level tar of named Docker volumes (ClickHouse
                        traces, MinIO media, Chroma once the vector store
                        exists). Postgres volumes are deliberately NOT tared
                        here - those go through pg_dump for consistent
                        snapshots.
* `OAUTH_TOKEN_DIR`   - tar of the host directory that will hold the Google
                        Calendar / Spotify refresh tokens once that feature
                        lands. Losing these means redoing BOTH OAuth consent
                        flows from scratch, so wire this the day the feature
                        ships.

Encryption uses `age` with a recipient public key only - the box can encrypt
but cannot decrypt, exactly what you want for unattended cron backups. The
matching age identity lives on offline media and is mounted only for
verify/restore drills.

Requires: Python 3.10+, the Docker CLI with the compose plugin, GNU tar, and
the `age` binary (`sudo apt-get install -y age`). Run as root (needed to
read `/var/lib/docker/volumes` and reach the compose project).
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("taylored-backup")

# Defaults mirror the prod compose project; override any of them in the conf
# file. Volume names are the FULL Docker names (project prefix included).
DEFAULTS: dict[str, str] = {
    "BACKUP_ROOT": "/var/backups/taylored-assistant",
    "DOCKER_PROJECT": "taylored-assistant-prod",
    "COMPOSE_FILES": (
        "infra/compose/docker-compose.base.yml,infra/compose/docker-compose.prod.yml"
    ),
    "COMPOSE_ENV_FILE": "infra/compose/.env.prod",
    "RETENTION_DAYS": "14",
    "APP_DB_SERVICE": "app-db",
    "APP_DB_USER": "assistant",
    "APP_DB_NAME": "assistant",
    "LANGFUSE_DB_SERVICE": "postgres",
    "LANGFUSE_DB_USER": "postgres",
    "LANGFUSE_DB_NAME": "postgres",
    # Comma-separated full volume names to tar (empty = none).
    "VOLUMES": (
        "taylored-assistant-prod_clickhouse_data,taylored-assistant-prod_minio_data"
    ),
    # Host dirs to tar (OAuth tokens once they exist; empty = skip).
    "OAUTH_TOKEN_DIR": "",
    "EXTRA_HOST_DIR": "",
    # age recipient public-key file; encryption REQUIRED unless --allow-plaintext.
    "AGE_RECIPIENT": "",
    # age identity file; only used by --verify / restore, normally NOT set.
    "AGE_IDENTITY": "",
}

# pg_dump flags shared by both Postgres dumps: custom format, no object owner
# (restores cleanly even if role names drift), gzip compression on the wire.
PG_DUMP_FLAGS = ["--format=custom", "--no-owner", "--compress=1"]


def load_config(path: Path) -> dict[str, str]:
    """Read a KEY=VALUE conf file over the built-in defaults.

    Args:
        path: Path to the conf file.

    Returns:
        Merged config; conf values win over `DEFAULTS`.
    """
    cfg: dict[str, str] = dict(DEFAULTS)
    if not path.exists():
        logger.warning("conf file %s not found - using built-in defaults", path)
        return cfg
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        cfg[key.strip()] = value.strip()
    return cfg


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    """Compute the SHA-256 hex digest of a file.

    Args:
        path: File to hash.
        chunk: Read size in bytes (streams large archives in memory-safe chunks).

    Returns:
        Lowercase hex digest.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def compose_command(cfg: dict[str, str], *args: str) -> list[str]:
    """Build a `docker compose` argv for the prod project.

    Args:
        cfg: Loaded config.
        args: Compose subcommand + arguments (e.g. `exec -T pg_dump ...`).

    Returns:
        argv list for `subprocess`. The env file is always passed so Compose
        can resolve every `${VAR:?...}` in the merged project.
    """
    command = ["docker", "compose", "-p", cfg["DOCKER_PROJECT"]]
    for path in cfg["COMPOSE_FILES"].split(","):
        command += ["-f", path.strip()]
    command += ["--env-file", cfg["COMPOSE_ENV_FILE"]]
    command += list(args)
    return command


def run_streaming(cmd: list[str], out_path: Path) -> None:
    """Run a command streaming stdout to a file; raise on non-zero exit.

    Args:
        cmd: argv list to run.
        out_path: File that receives stdout.

    Raises:
        RuntimeError: if the command exits non-zero (its stderr is logged).
    """
    logger.info("running: %s", " ".join(cmd))
    with out_path.open("wb") as out_handle:
        result = subprocess.run(
            cmd, stdout=out_handle, stderr=subprocess.PIPE, text=True, check=False
        )
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")


def run_silent(cmd: list[str]) -> str:
    """Run a short command and return its trimmed stdout.

    Args:
        cmd: argv list to run.

    Returns:
        Trailing-whitespace-stripped stdout.

    Raises:
        RuntimeError: if the command exits non-zero (its stderr is logged).
    """
    logger.info("running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.stdout.strip()


def encrypt_file(plain: Path, encrypted: Path, recipient_file: str) -> None:
    """age-encrypt a plaintext file into a `.age` file.

    Args:
        plain: Plaintext staging file.
        encrypted: Destination `.age` path.
        recipient_file: age public-key file (contains an `age1...` key).

    Raises:
        RuntimeError: if age is missing or encryption fails.
    """
    cmd = ["age", "-R", recipient_file, "-o", str(encrypted), str(plain)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"age encryption failed for {plain.name}")


def volume_mountpoint(volume: str) -> str:
    """Resolve a Docker volume's host mountpoint.

    Args:
        volume: Full Docker volume name.

    Returns:
        Host path of the volume's `_data` directory.

    Raises:
        RuntimeError: if the volume does not exist on this host.
    """
    cmd = ["docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"]
    try:
        return run_silent(cmd)
    except RuntimeError as error:
        raise RuntimeError(f"volume '{volume}' not found - is the stack up?") from error


def pg_dump_item(cfg: dict[str, str], kind: str, stage: Path) -> Path:
    """pg_dump one Postgres database into the staging dir.

    Args:
        cfg: Loaded config.
        kind: `"app"` or `"langfuse"` - selects service/user/db and name.
        stage: Staging directory to write into.

    Returns:
        Path of the produced plaintext dump.

    Raises:
        RuntimeError: if the dump fails.
    """
    prefix = "app" if kind == "app" else "langfuse"
    service = cfg["APP_DB_SERVICE" if kind == "app" else "LANGFUSE_DB_SERVICE"]
    user = cfg["APP_DB_USER" if kind == "app" else "LANGFUSE_DB_USER"]
    database = cfg["APP_DB_NAME" if kind == "app" else "LANGFUSE_DB_NAME"]
    out = stage / f"{prefix}-postgres.dump"
    cmd = compose_command(cfg, "exec", "-T", service, "pg_dump")
    cmd += ["-U", user, "-d", database, *PG_DUMP_FLAGS]
    run_streaming(cmd, out)
    return out


def tar_path_to(src: Path, out: Path) -> None:
    """Tar + gzip a host path (file or directory) into `out`.

    Args:
        src: Host path to archive. A directory is archived by its contents.
        out: Destination `.tar.gz`.

    Raises:
        RuntimeError: if tar fails.
    """
    cmd = ["tar", "-czf", str(out), "-C", str(src.parent), src.name]
    run_streaming(cmd, out)


def volume_tar_item(cfg: dict[str, str], volume: str, stage: Path) -> Path:
    """Tar a named Docker volume into the staging dir.

    Args:
        cfg: Loaded config (kept for symmetry with the other producers).
        volume: Full Docker volume name.
        stage: Staging directory to write into.

    Returns:
        Path of the produced `.tar.gz`.

    Raises:
        RuntimeError: if the volume is missing or tar fails.
    """
    mountpoint = Path(volume_mountpoint(volume))
    out = stage / f"volume-{volume.replace('/', '_')}.tar.gz"
    tar_path_to(mountpoint, out)
    return out


def seal_item(
    plain: Path, cfg: dict[str, str], allow_plaintext: bool
) -> tuple[str, str]:
    """Turn one staging file into its final archive: name + plaintext SHA-256.

    Hashes the plaintext BEFORE sealing (the manifest records decrypted
    hashes, so verify can compare without trusting the age file's metadata).
    Encryption requires an age recipient; the plaintext is then deleted.

    Args:
        plain: Plaintext staging file.
        cfg: Loaded config.
        allow_plaintext: If true, keep the file unencrypted (testing only).

    Returns:
        `(final_name, sha256_of_decrypted_contents)` where final_name ends in
        `.age` unless plaintext mode is on.

    Raises:
        RuntimeError: if encryption is required but no recipient is configured
            or the recipient file is missing.
    """
    digest = sha256_file(plain)
    if allow_plaintext:
        logger.warning("PLAINTEXT backup (no encryption) - testing only")
        return plain.name, digest
    recipient = cfg["AGE_RECIPIENT"]
    if not recipient:
        raise RuntimeError(
            "AGE_RECIPIENT is empty and --allow-plaintext not given; refusing an unencrypted backup"
        )
    recipient_path = Path(recipient)
    if not recipient_path.exists():
        raise RuntimeError(f"age recipient file not found: {recipient}")
    encrypted = plain.with_name(plain.name + ".age")
    encrypt_file(plain, encrypted, recipient)
    plain.unlink(missing_ok=True)
    return encrypted.name, digest


def retention_cleanup(cfg: dict[str, str]) -> None:
    """Delete dated backup dirs older than the retention window.

    Args:
        cfg: Loaded config (`RETENTION_DAYS`; 0 keeps nothing, negative keeps all).
    """
    keep = int(cfg["RETENTION_DAYS"])
    daily = Path(cfg["BACKUP_ROOT"]) / "daily"
    if not daily.exists():
        return
    dirs = sorted(p for p in daily.iterdir() if p.is_dir())
    if keep < 0:
        return
    for old in dirs[:-keep] if keep else dirs:
        logger.info("retention: removing %s", old)
        shutil.rmtree(old)


def write_manifest(day_dir: Path, entries: list[tuple[str, str]]) -> None:
    """Write manifest.txt: one `sha256  filename` line per sealed item.

    Args:
        day_dir: Destination backup directory.
        entries: `(final_name, sha256_of_decrypted_contents)` pairs.
    """
    lines = [
        "# taylored-assistant backup manifest",
        "# sha256 is of the DECRYPTED contents (verify recomputes it via age -d)",
    ]
    for name, digest in sorted(entries):
        lines.append(f"{digest}  {name}")
    (day_dir / "manifest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_latest(cfg: dict[str, str]) -> None:
    """Decrypt + hash the newest backup dir and compare to its manifest.

    Args:
        cfg: Loaded config (requires `AGE_IDENTITY`).

    Raises:
        RuntimeError: if no identity is configured, no backup exists, or any
            item fails to decrypt or its hash mismatches.
    """
    identity = cfg["AGE_IDENTITY"]
    if not identity:
        raise RuntimeError(
            "AGE_IDENTITY is empty - mount the offline identity to verify (see BACKUP.md)"
        )
    daily = Path(cfg["BACKUP_ROOT"]) / "daily"
    dirs = sorted(p for p in daily.iterdir() if p.is_dir()) if daily.exists() else []
    if not dirs:
        raise RuntimeError(f"no backup found under {daily}")
    latest = dirs[-1]

    manifest: dict[str, str] = {}
    manifest_file = latest / "manifest.txt"
    if not manifest_file.exists():
        raise RuntimeError(f"no manifest.txt in {latest}")
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        digest, _, name = line.partition("  ")
        manifest[name] = digest

    checked = 0
    for item in sorted(latest.iterdir()):
        if not item.name.endswith(".age"):
            continue
        expected = manifest.get(item.name)
        if expected is None:
            logger.error("%s missing from manifest", item.name)
            continue
        digest = hashlib.sha256()
        cmd = ["age", "-d", "-i", identity, "-o", "-", str(item)]
        with subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as proc:
            assert proc.stdout is not None
            while True:
                block = proc.stdout.read(1 << 20)
                if not block:
                    break
                digest.update(block)
        if proc.returncode != 0:
            raise RuntimeError(f"age decryption failed for {item.name}")
        if digest.hexdigest() != expected:
            raise RuntimeError(f"SHA-256 MISMATCH for {item.name}")
        logger.info("OK   %s (decrypted sha256 matches manifest)", item.name)
        checked += 1
    if checked == 0:
        logger.warning("no .age items found to verify in %s", latest)


def run_backup(cfg: dict[str, str], allow_plaintext: bool) -> int:
    """Execute one backup run: produce, seal, manifest, retention.

    Args:
        cfg: Loaded config.
        allow_plaintext: Whether unencrypted output is permitted (testing).

    Returns:
        Process exit code: 0 on success, 1 if any item failed.
    """
    root = Path(cfg["BACKUP_ROOT"])
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    day_dir = root / "daily" / day
    stage = day_dir / ".stage"
    stage.mkdir(parents=True, exist_ok=True)

    producers: list[tuple[str, object]] = [
        ("app-postgres", lambda: pg_dump_item(cfg, "app", stage)),
        ("langfuse-postgres", lambda: pg_dump_item(cfg, "langfuse", stage)),
    ]
    for volume in (v for v in cfg["VOLUMES"].split(",") if v.strip()):
        producers.append(
            ("volume " + volume, lambda v=volume: volume_tar_item(cfg, v, stage))
        )
    oauth_dir = cfg["OAUTH_TOKEN_DIR"]
    if oauth_dir and Path(oauth_dir).exists():
        producers.append(
            (
                "oauth-tokens",
                lambda: tar_path_to(Path(oauth_dir), stage / "oauth-tokens.tar.gz"),
            )
        )
    extra_dir = cfg["EXTRA_HOST_DIR"]
    if extra_dir and Path(extra_dir).exists():
        producers.append(
            (
                "extra " + extra_dir,
                lambda: tar_path_to(Path(extra_dir), stage / "extra-host.tar.gz"),
            )
        )

    plaintexts: list[Path] = []
    errors: list[str] = []
    for label, producer in producers:
        try:
            plaintexts.append(producer())  # type: ignore[operator]
        except RuntimeError as error:
            errors.append(f"{label}: {error}")
            logger.error("item failed: {label}: {error}")

    entries: list[tuple[str, str]] = []
    for plain in plaintexts:
        try:
            entries.append(seal_item(plain, cfg, allow_plaintext))
        except RuntimeError as error:
            errors.append(str(error))
            plain.unlink(missing_ok=True)

    for name, _digest in entries:
        shutil.move(str(stage / name), str(day_dir / name))
    write_manifest(day_dir, entries)
    shutil.rmtree(stage, ignore_errors=True)
    retention_cleanup(cfg)

    if errors:
        for error in errors:
            logger.error("failed: %s", error)
        logger.error(
            "backup finished WITH errors - investigate (see infra/backup/BACKUP.md)"
        )
        return 1
    logger.info("backup complete: %s (%d items)", day_dir, len(entries))
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to `sys.argv[1:]`).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Encrypted, scheduled backups for the prod stack (M28)."
    )
    here = Path(__file__).resolve().parent
    parser.add_argument(
        "--config",
        type=Path,
        default=here / "backup.conf",
        help="KEY=VALUE conf file (default: backup.conf next to this script)",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="verify the newest backup (decrypt + hash vs manifest.txt)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resolved config and exit without writing anything",
    )
    parser.add_argument(
        "--allow-plaintext",
        action="store_true",
        help="write UNENCRYPTED backups (testing only; refused by default)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = load_config(args.config)
    if args.dry_run:
        for key in sorted(cfg):
            logger.info("%s = %s", key, cfg[key])
        return 0
    if args.verify:
        verify_latest(cfg)
        return 0
    return run_backup(cfg, args.allow_plaintext)


if __name__ == "__main__":
    sys.exit(main())
