#!/usr/bin/env python3
"""Restore a dated encrypted backup produced by backup.py (M28).

Shares the SAME conf file as backup.py so one set of keys drives both sides.
Restoring requires the age IDENTITY (private key) - keep it on offline media
and mount it only for a drill or a real recovery.

Two operation modes:

* DRILL (default, safe): nothing live is touched. Postgres dumps are restored
  into disposable databases named `<live-db>_restore_<date>`, volumes into
  disposable volumes named `<volume>_restore_<date>`, and host-dir tars into
  `<BACKUP_ROOT>/restore/<date>/`. Run this monthly to prove your backups
  actually work.
* LIVE RECOVERY (--to-live --yes): Postgres is restored into the real database
  and tars into the real Docker volumes / host dirs. STOP the relevant compose
  services first (see infra/backup/BACKUP.md) - restoring into a running DB
  or a mounted volume produces a torn result.

Item types handled (filename decides the strategy):

* `app-postgres.dump` / `langfuse-postgres.dump` - pg_restore into the DB named
  by APP_DB_* / LANGFUSE_DB_* (or a `_restore_<date>` clone for a drill).
* `volume-<name>.tar.gz` - extracted into the named Docker volume (or a
  `_restore_<date>` clone for a drill).
* `oauth-tokens.tar.gz` / `extra-host.tar.gz` - extracted into the host dirs
  from OAUTH_TOKEN_DIR / EXTRA_HOST_DIR (or the drill area).

Decrypted plaintext is removed when the run finishes unless --keep-plaintext is
given, so a restore never leaves secrets lying around on the box.

Requires: Python 3.10+, the Docker CLI with the compose plugin, GNU tar, and
the `age` binary. Run as root.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("taylored-restore")

# Same knobs as backup.py; see DEFAULTS there for the meaning of each key.
DEFAULTS: dict[str, str] = {
    "BACKUP_ROOT": "/var/backups/taylored-assistant",
    "DOCKER_PROJECT": "taylored-assistant-prod",
    "COMPOSE_FILES": (
        "infra/compose/docker-compose.base.yml,infra/compose/docker-compose.prod.yml"
    ),
    "COMPOSE_ENV_FILE": "infra/compose/.env.prod",
    "APP_DB_SERVICE": "app-db",
    "APP_DB_USER": "assistant",
    "APP_DB_NAME": "assistant",
    "LANGFUSE_DB_SERVICE": "postgres",
    "LANGFUSE_DB_USER": "postgres",
    "LANGFUSE_DB_NAME": "postgres",
    "OAUTH_TOKEN_DIR": "",
    "EXTRA_HOST_DIR": "",
    "AGE_IDENTITY": "",
}

# pg_restore flags shared by both databases: replace existing objects and drop
# the table ownership requirement (roles on a fresh host may differ).
PG_RESTORE_FLAGS = ["--format=custom", "--no-owner", "--clean", "--if-exists"]


def load_config(path: Path) -> dict[str, str]:
    """Read a KEY=VALUE conf file over the built-in defaults.

    Args:
        path: Path to the conf file (shared with backup.py).

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


def compose_command(cfg: dict[str, str], *args: str) -> list[str]:
    """Build a `docker compose` argv for the prod project.

    Args:
        cfg: Loaded config.
        args: Compose subcommand + arguments.

    Returns:
        argv list for `subprocess`. The env file is passed so Compose can
        resolve every `${VAR:?...}` in the merged project.
    """
    command = ["docker", "compose", "-p", cfg["DOCKER_PROJECT"]]
    for path in cfg["COMPOSE_FILES"].split(","):
        command += ["-f", path.strip()]
    command += ["--env-file", cfg["COMPOSE_ENV_FILE"]]
    command += list(args)
    return command


def run_quiet(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command capturing output; raise on non-zero when requested.

    Args:
        cmd: argv list to run.
        check: If true, raise `RuntimeError` on a non-zero exit.

    Returns:
        The `CompletedProcess`.

    Raises:
        RuntimeError: if `check` and the exit code is non-zero.
    """
    logger.info("running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result


def run_stream_in(cmd: list[str], stdin_path: Path) -> None:
    """Run a command feeding a local file to its stdin; raise on non-zero.

    Lets `docker compose exec -T ... pg_restore` read a host dump through the
    container's stdin without copying the file inside the container.

    Args:
        cmd: argv list to run.
        stdin_path: Local file piped to the command's stdin.

    Raises:
        RuntimeError: if the command exits non-zero (its stderr is logged).
    """
    logger.info("running: %s < %s", " ".join(cmd), stdin_path)
    with stdin_path.open("rb") as stdin_handle:
        result = subprocess.run(
            cmd,
            stdin=stdin_handle,
            capture_output=True,
            text=True,
            check=False,
        )
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")


def decrypt_item(cfg: dict[str, str], age_file: Path, out: Path) -> None:
    """age-decrypt one `.age` backup item to a plaintext file.

    Args:
        cfg: Loaded config (requires `AGE_IDENTITY`).
        age_file: The `.age` file to decrypt.
        out: Destination plaintext path.

    Raises:
        RuntimeError: if no identity is configured or decryption fails.
    """
    identity = cfg["AGE_IDENTITY"]
    if not identity:
        raise RuntimeError(
            "AGE_IDENTITY is empty - mount the offline identity to restore (see BACKUP.md)"
        )
    identity_path = Path(identity)
    if not identity_path.exists():
        raise RuntimeError(f"age identity file not found: {identity}")
    cmd = ["age", "-d", "-i", identity, "-o", str(out), str(age_file)]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        for line in result.stderr.splitlines():
            logger.error("  %s", line)
        raise RuntimeError(f"age decryption failed for {age_file.name}")


def list_backups(cfg: dict[str, str]) -> list[Path]:
    """Return dated backup dirs newest-first.

    Args:
        cfg: Loaded config.

    Returns:
        Sorted `daily` directories, newest first.
    """
    daily = Path(cfg["BACKUP_ROOT"]) / "daily"
    if not daily.exists():
        return []
    return sorted((p for p in daily.iterdir() if p.is_dir()), reverse=True)


def date_dirs(cfg: dict[str, str]) -> None:
    """Print every dated backup with its manifest (the `list` command).

    Args:
        cfg: Loaded config.
    """
    dirs = list_backups(cfg)
    if not dirs:
        logger.info("no backups under %s/daily", cfg["BACKUP_ROOT"])
        return
    for day_dir in dirs:
        manifest = day_dir / "manifest.txt"
        logger.info("%s", day_dir)
        if manifest.exists():
            for line in manifest.read_text(encoding="utf-8").splitlines():
                if not line.startswith("#"):
                    logger.info("    %s", line)


def resolve_db_target(
    cfg: dict[str, str], prefix: str, live: bool, date: str
) -> tuple[str, str, str]:
    """Return (service, user, target_db) for a Postgres item.

    Args:
        cfg: Loaded config.
        prefix: `"app"` or `"langfuse"` - which DB the dump came from.
        live: If true, target the real database; else a `_restore_<date>` clone.
        date: Backup date used to name the drill clone.

    Returns:
        `(service, user, target_db_name)`.
    """
    is_app = prefix == "app"
    service = cfg["APP_DB_SERVICE" if is_app else "LANGFUSE_DB_SERVICE"]
    user_key = "APP_DB_USER" if is_app else "LANGFUSE_DB_USER"
    db_key = "APP_DB_NAME" if is_app else "LANGFUSE_DB_NAME"  # pragma: allowlist secret
    user = cfg[user_key]
    live_db = cfg[db_key]
    target = live_db if live else f"{live_db}_restore_{date}"
    return service, user, target


def ensure_database(
    cfg: dict[str, str], service: str, user: str, database: str
) -> None:
    """Create a target database if it does not already exist.

    Args:
        cfg: Loaded config.
        service: Compose service hosting Postgres.
        user: Role used to connect.
        database: Database to create.

    Raises:
        RuntimeError: if the create fails for a reason other than "exists".
    """
    cmd = compose_command(
        cfg,
        "exec",
        "-T",
        service,
        "psql",
        "-U",
        user,
        "-d",
        "postgres",
        "-tAc",
        f"SELECT 1 FROM pg_database WHERE datname='{database}'",
    )
    result = run_quiet(cmd, check=False)
    if result.stdout.strip() == "1":
        logger.info("database %s already exists", database)
        return
    create = compose_command(
        cfg,
        "exec",
        "-T",
        service,
        "psql",
        "-U",
        user,
        "-d",
        "postgres",
        "-c",
        f"CREATE DATABASE {database}",
    )
    run_quiet(create)


def restore_postgres(
    cfg: dict[str, str], name: str, plain: Path, live: bool, date: str
) -> None:
    """pg_restore a dump into the target database for the given item.

    Args:
        cfg: Loaded config.
        name: Backup file stem (e.g. `app-postgres.dump`) - selects the DB.
        plain: Decrypted dump file.
        live: If true, target the real database (replace contents).
        date: Backup date used to name the drill clone.

    Raises:
        RuntimeError: if the database setup or restore fails.
    """
    prefix = name.split("-", 1)[0]
    service, user, database = resolve_db_target(cfg, prefix, live, date)
    logger.info("restoring %s into %s (service %s)", name, database, service)
    ensure_database(cfg, service, user, database)
    cmd = compose_command(
        cfg,
        "exec",
        "-T",
        service,
        "pg_restore",
        "-U",
        user,
        "-d",
        database,
        *PG_RESTORE_FLAGS,
    )
    run_stream_in(cmd, plain)


def volume_name_from_file(name: str) -> str:
    """Recover a Docker volume name from a `volume-<name>.tar.gz` stem.

    Args:
        name: File stem produced by backup.py.

    Returns:
        The full Docker volume name.

    Raises:
        ValueError: if the stem is not a volume tar.
    """
    if not name.startswith("volume-") or not name.endswith(".tar.gz"):
        raise ValueError(f"not a volume tar: {name}")
    return name[len("volume-") : -len(".tar.gz")]


def ensure_volume(cfg: dict[str, str], volume: str) -> str:
    """Create a Docker volume if needed and return its host mountpoint.

    Args:
        cfg: Loaded config (unused; kept for interface symmetry).
        volume: Full Docker volume name.

    Returns:
        Host path of the volume's `_data` directory.
    """
    result = run_quiet(["docker", "volume", "create", volume], check=False)
    if result.returncode != 0:
        # "already exists" is the only benign failure here.
        run_quiet(["docker", "volume", "inspect", volume])
    mountpoint = subprocess.run(
        ["docker", "volume", "inspect", volume, "--format", "{{.Mountpoint}}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return mountpoint


def extract_tar(tar_path: Path, dest: Path) -> None:
    """Extract a tar.gz into a destination directory.

    Args:
        tar_path: The `.tar.gz` archive.
        dest: Directory to extract into (created if missing).

    Raises:
        RuntimeError: if extraction fails.
    """
    dest.mkdir(parents=True, exist_ok=True)
    run_quiet(["tar", "-xzf", str(tar_path), "-C", str(dest)])


def restore_volume(
    cfg: dict[str, str], name: str, plain: Path, live: bool, date: str
) -> None:
    """Restore a volume tar into the target Docker volume.

    Args:
        cfg: Loaded config.
        name: File stem (e.g. `volume-taylored-assistant-prod_minio_data.tar.gz`).
        plain: Decrypted archive.
        live: If true, target the real volume; else a `_restore_<date>` clone.
        date: Backup date used to name the drill clone.

    Raises:
        RuntimeError: if the volume setup or extraction fails.
    """
    volume = volume_name_from_file(name)
    target = volume if live else f"{volume}_restore_{date}"
    logger.info("restoring %s into volume %s", name, target)
    mountpoint = ensure_volume(cfg, target)
    extract_tar(plain, Path(mountpoint))


def restore_host_dir(
    cfg: dict[str, str], name: str, plain: Path, live: bool, date: str
) -> None:
    """Restore an oauth/extra host-dir tar to its configured or drill location.

    Args:
        cfg: Loaded config.
        name: File stem (`oauth-tokens.tar.gz` or `extra-host.tar.gz`).
        plain: Decrypted archive.
        live: If true, extract into the real host dir; else into the drill area.
        date: Backup date used to name the drill area.

    Raises:
        RuntimeError: if restoring live and no dir is configured.
    """
    if name.startswith("oauth"):
        live_dir = cfg["OAUTH_TOKEN_DIR"]
        kind = "oauth-tokens"
    else:
        live_dir = cfg["EXTRA_HOST_DIR"]
        kind = "extra-host"
    if live:
        if not live_dir:
            raise RuntimeError(f"{name}: no {kind.upper()} configured to restore into")
        dest = Path(live_dir)
        logger.info("restoring %s into host dir %s", name, dest)
        extract_tar(plain, dest)
    else:
        dest = Path(cfg["BACKUP_ROOT"]) / "restore" / date / kind
        logger.info("restoring %s into drill dir %s", name, dest)
        extract_tar(plain, dest)


def run_restore(cfg: dict[str, str], date: str, live: bool) -> None:
    """Decrypt and restore every item in one dated backup.

    Args:
        cfg: Loaded config.
        date: `YYYY-MM-DD` backup to restore from.
        live: If true, restore into the real databases/volumes/dirs.

    Raises:
        RuntimeError: if the backup dir is missing or any item fails.
    """
    dirs = list_backups(cfg)
    by_date = {d.name: d for d in dirs}
    if date not in by_date:
        raise RuntimeError(
            f"no backup for {date}; available: {', '.join(sorted(by_date)) or 'none'}"
        )
    day_dir = by_date[date]
    stage = Path(cfg["BACKUP_ROOT"]) / "restore" / date / ".stage"
    stage.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    restored: list[str] = []
    for item in sorted(day_dir.iterdir()):
        if not item.name.endswith(".age"):
            continue
        name = item.name[: -len(".age")]
        plain = stage / name
        try:
            decrypt_item(cfg, item, plain)
            if name.endswith("-postgres.dump"):
                restore_postgres(cfg, name, plain, live, date)
            elif name.endswith(".tar.gz") and name.startswith("volume-"):
                restore_volume(cfg, name, plain, live, date)
            elif name == "oauth-tokens.tar.gz" or name == "extra-host.tar.gz":
                restore_host_dir(cfg, name, plain, live, date)
            else:
                errors.append(f"{name}: unknown item type - skipped")
                continue
            restored.append(name)
            logger.info("restored %s", name)
        except RuntimeError as error:
            errors.append(f"{name}: {error}")
            logger.error("item failed: {name}: {error}")
        finally:
            plain.unlink(missing_ok=True)

    shutil.rmtree(stage, ignore_errors=True)
    if errors:
        for error in errors:
            logger.error("failed: %s", error)
        logger.error(
            "restore finished WITH errors - investigate (see infra/backup/BACKUP.md)"
        )
        raise RuntimeError("restore incomplete")
    logger.info("restore complete: %s (%d items)", date, len(restored))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argv override (defaults to `sys.argv[1:]`).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        description="Restore a dated encrypted backup produced by backup.py (M28)."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "backup.conf",
        help="KEY=VALUE conf file shared with backup.py",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list dated backups and their manifests")

    restore_p = sub.add_parser("restore", help="restore one dated backup")
    restore_p.add_argument(
        "--date",
        default=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        help="YYYY-MM-DD to restore (default: today's dir, if any)",
    )
    restore_p.add_argument(
        "--to-live",
        action="store_true",
        help="restore into the REAL databases/volumes/dirs (default: disposable drill targets)",
    )
    restore_p.add_argument(
        "--yes",
        action="store_true",
        help="confirm a --to-live restore (never asked otherwise)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    cfg = load_config(args.config)
    if args.command == "list":
        date_dirs(cfg)
        return 0
    if args.to_live and not args.yes:
        logger.error("--to-live touches real data - pass --yes to confirm")
        return 1
    try:
        run_restore(cfg, args.date, args.to_live)
    except RuntimeError as error:
        logger.error("restore failed: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
