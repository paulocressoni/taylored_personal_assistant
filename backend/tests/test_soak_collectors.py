"""Tests for the soak collector shell script.

The collectors cannot be exercised end to end without a board and a running
stack, so the seam under test is `plan`: it resolves the same streams the start
would use and prints the command it would run. That makes the two decisions
that silently ruin a capture — which serial device, and whether to append —
assertable in CI, where neither a device nor Docker exists.

`bash -n` is always run. `shellcheck` is run when it is installed and skipped
otherwise: it is not part of this project's toolchain, and a hard requirement
would turn a missing linter into a failing test.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "soak_collectors.sh"
# Pinned so a test addresses a known directory instead of a timestamp.
RUN = "20260101T000000Z"


def _run(
    args: list[str], env: dict[str, str], soak_dir: Path
) -> subprocess.CompletedProcess:
    """Run the collector with a clean environment rooted in a temp directory.

    Args:
        args: Subcommand and its arguments.
        env: Extra environment for the run.
        soak_dir: Where the collectors would write, one directory per run.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(soak_dir),
            "SOAK_DIR": str(soak_dir),
            "SOAK_REPO": str(soak_dir),
            "SOAK_RUN_ID": RUN,
            **env,
        },
    )


def _run_dir(soak: Path) -> Path:
    """Return the run directory the collector addresses in these tests."""
    return soak / RUN


def _fake_device(root: Path, name: str, target: str) -> None:
    """Create a by-id style symlink pointing at a fake device node."""
    node = root / "dev" / target
    node.parent.mkdir(parents=True, exist_ok=True)
    node.touch()
    (root / "by-id").mkdir(exist_ok=True)
    (root / "by-id" / name).symlink_to(node)


def test_the_script_parses(tmp_path: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_shellcheck_passes_when_it_is_installed(tmp_path: Path) -> None:
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck is not installed; bash -n above is the gate")

    result = subprocess.run(
        ["shellcheck", str(SCRIPT)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stdout


def test_plan_resolves_three_streams_and_the_serial_device(tmp_path: Path) -> None:
    _fake_device(
        tmp_path, "usb-Espressif_USB_JTAG_serial_debug_unit_AA-if00", "ttyACM0"
    )
    soak = tmp_path / "soak"
    soak.mkdir()

    result = _run(["plan"], {"SOAK_SERIAL_DIR": str(tmp_path / "by-id")}, soak)

    assert result.returncode == 0, result.stderr
    assert "satellite: port" in result.stdout
    assert "usb-Espressif" in result.stdout
    assert result.stdout.count("backend container") == 1
    assert result.stdout.count("frontend container") == 1
    assert "satellite serial" in result.stdout


def test_plan_omits_the_satellite_when_switched_off(tmp_path: Path) -> None:
    _fake_device(tmp_path, "usb-Espressif_X-if00", "ttyACM0")
    soak = tmp_path / "soak"
    soak.mkdir()

    result = _run(
        ["plan"],
        {"SOAK_SERIAL_DIR": str(tmp_path / "by-id"), "SOAK_SATELLITE": "0"},
        soak,
    )

    assert result.returncode == 0, result.stderr
    assert "satellite: skipped (SOAK_SATELLITE=0)" in result.stdout
    assert "satellite serial" not in result.stdout


def test_plan_appends_only_when_the_flag_is_set(tmp_path: Path) -> None:
    # `script` truncates without -a, so an append run that forgot -a would wipe
    # the capture while the two docker logs streams appended to theirs.
    _fake_device(tmp_path, "usb-Espressif_X-if00", "ttyACM0")
    soak = tmp_path / "soak"
    soak.mkdir()
    env = {"SOAK_SERIAL_DIR": str(tmp_path / "by-id")}

    plain = _run(["plan"], env, soak)
    appending = _run(["plan"], {**env, "SOAK_APPEND": "1"}, soak)

    assert " -a " not in plain.stdout
    assert " -a " in appending.stdout


def test_plan_refuses_two_different_devices(tmp_path: Path) -> None:
    # Two boards on one host is a question only the operator can answer;
    # attributing a soak to the wrong one is worse than not starting.
    _fake_device(tmp_path, "usb-Espressif_A-if00", "ttyACM0")
    _fake_device(tmp_path, "usb-Espressif_B-if00", "ttyACM1")
    soak = tmp_path / "soak"
    soak.mkdir()

    result = _run(["plan"], {"SOAK_SERIAL_DIR": str(tmp_path / "by-id")}, soak)

    assert "refusing to guess" in result.stderr
    assert "SOAK_SATELLITE_PORT" in result.stderr


def test_plan_deduplicates_two_links_to_one_device(tmp_path: Path) -> None:
    # A composite USB device publishes several by-id links to the same node;
    # counting links instead of nodes would refuse every ordinary soak.
    _fake_device(tmp_path, "usb-Espressif_A-if00", "ttyACM0")
    _fake_device(tmp_path, "usb-Espressif_A-if02", "ttyACM0")
    soak = tmp_path / "soak"
    soak.mkdir()

    result = _run(["plan"], {"SOAK_SERIAL_DIR": str(tmp_path / "by-id")}, soak)

    assert "refusing to guess" not in result.stderr
    assert "satellite: port" in result.stdout


def test_preflight_fails_fast_without_a_device(tmp_path: Path) -> None:
    soak = tmp_path / "soak"
    soak.mkdir()

    result = _run(["preflight"], {"SOAK_SERIAL_DIR": str(tmp_path / "by-id")}, soak)

    assert result.returncode == 1
    assert "SOAK_SATELLITE=0" in result.stderr
    assert not (soak / "current").exists()


def test_preflight_refuses_to_append_over_a_capture(tmp_path: Path) -> None:
    soak = tmp_path / "soak"
    soak.mkdir()
    run = _run_dir(soak)
    run.mkdir(parents=True, exist_ok=True)
    (run / "backend.log").write_text("existing capture\n", encoding="utf-8")

    result = _run(["preflight"], {"SOAK_SATELLITE": "0"}, soak)

    assert result.returncode == 1
    assert "already has bytes" in result.stderr
    assert "SOAK_APPEND=1" in result.stderr


def test_meta_folds_the_resolved_plan_into_meta(tmp_path: Path) -> None:
    # META must describe the device this capture used. preflight writes the
    # resolved facts to `plan`, and `meta` is the only writer of META — so a
    # later `make soak-meta` cannot silently drop them.
    soak = tmp_path / "soak"
    soak.mkdir()
    run = _run_dir(soak)
    run.mkdir(parents=True, exist_ok=True)
    (run / "plan").write_text(
        "satellite=1\nsatellite_port=/dev/serial/by-id/usb-X-if00\n", encoding="utf-8"
    )

    written = _run(["meta"], {"SOAK_SATELLITE": "0"}, soak)

    assert written.returncode == 0, written.stderr
    meta = (run / "META").read_text(encoding="utf-8")
    assert "satellite=1" in meta
    assert "satellite_port=/dev/serial/by-id/usb-X-if00" in meta


def _fake_docker(root: Path) -> Path:
    """Write a `docker` stand-in that behaves like a running stack.

    Enough of the CLI for `start` and `stop`: `ps` is empty, `inspect` returns
    an id, and `logs -f` emits one line and then holds the pipe open so the
    collector looks like a live follower.

    Args:
        root: Directory to place the stub and its bin dir in.

    Returns:
        The directory to prepend to `PATH`.
    """
    bin_dir = root / "fakebin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(
        """#!/usr/bin/env bash
case "$1" in
    ps) exit 0 ;;
    inspect)
        for arg in "$@"; do
            if [ "$arg" = "{{.Id}}" ]; then echo deadbeefcafe; exit 0; fi
        done
        exit 0 ;;
    logs) echo "2026-01-01T00:00:01.000000001Z {\\"message\\": \\"voice session open\\"}"; sleep 60 ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir


def test_start_and_stop_manage_the_group_and_report_the_skip(tmp_path: Path) -> None:
    # The satellite skip line must print the RESOLVED value: `?=` also picks up
    # a shell export, and the operator who forgot theirs should see why.
    bin_dir = _fake_docker(tmp_path)
    soak = tmp_path / "soak"
    soak.mkdir()
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SOAK_SATELLITE": "0",
        "SOAK_FIRST_LINE_S": "5",
    }

    started = _run(["start"], env, soak)
    try:
        assert started.returncode == 0, started.stderr
        assert "satellite: skipped (SOAK_SATELLITE=0)" in started.stdout
        assert "make soak-report UNTIL=<ISO> SINCE=<ISO>" in started.stdout
        run = _run_dir(soak)
        assert (run / "backend.pgid").exists()
        assert (run / "frontend.pgid").exists()
        assert "satellite.pgid" not in [p.name for p in run.iterdir()]
        assert (soak / "current").read_text(encoding="utf-8").strip() == RUN
    finally:
        stopped = _run(["stop"], env, soak)

    assert "backend: stopped" in stopped.stdout
    assert "frontend: stopped" in stopped.stdout
    assert not (_run_dir(soak) / "backend.pgid").exists()


def _sourced(function: str, *args: str) -> subprocess.CompletedProcess:
    """Call one of the script's helpers by sourcing it.

    The script dispatches its subcommands only when executed, so a test can
    reach the predicates directly instead of guessing at them through `plan`.

    Args:
        function: Helper name.
        args: Its arguments.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["bash", "-c", f'. "{SCRIPT}" && {function} "$@"', function, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_a_failed_start_is_deleted_only_when_nothing_was_captured(
    tmp_path: Path,
) -> None:
    # `script` writes its header before the child runs, so a start that never
    # produced firmware output leaves a small file. Deleting it lets a retry
    # proceed without a manual rm — but a failed cmake reconfigure prints ~110
    # lines and not one of them is a firmware line, so a "no `I (`" test would
    # delete exactly the output an operator needs.
    header_only = tmp_path / "header-only.log"
    header_only.write_text(
        "Script started on 2026-01-01 00:00:00+00:00\n\n"
        "Script done on 2026-01-01 00:00:01+00:00\n",
        encoding="utf-8",
    )
    failed_cmake = tmp_path / "failed-cmake.log"
    failed_cmake.write_text(
        "Script started on 2026-01-01 00:00:00+00:00\n"
        "Running cmake in directory /repo/firmware/satellite/build\n"
        "CMake Error: could not find toolchain\n"
        "Script done on 2026-01-01 00:00:01+00:00\n",
        encoding="utf-8",
    )

    noise = _sourced("_only_script_noise", str(header_only))
    kept = _sourced("_only_script_noise", str(failed_cmake))

    assert noise.returncode == 0
    assert kept.returncode == 1


def test_two_runs_get_two_directories(tmp_path: Path) -> None:
    # The whole point of the run directory: a second capture cannot overwrite,
    # interleave with or append to the first, and each log's path names the run
    # it came from.
    bin_dir = _fake_docker(tmp_path)
    soak = tmp_path / "soak"
    soak.mkdir()
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "SOAK_SATELLITE": "0",
        "SOAK_FIRST_LINE_S": "5",
    }

    first = _run(["start"], {**base, "SOAK_RUN_ID": "20260101T000000Z"}, soak)
    _run(["stop"], {**base, "SOAK_RUN_ID": "20260101T000000Z"}, soak)
    second = _run(["start"], {**base, "SOAK_RUN_ID": "20260102T000000Z"}, soak)
    _run(["stop"], {**base, "SOAK_RUN_ID": "20260102T000000Z"}, soak)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert "already has bytes" not in second.stderr
    assert (soak / "20260101T000000Z" / "backend.log").exists()
    assert (soak / "20260102T000000Z" / "backend.log").exists()
    assert (soak / "current").read_text(encoding="utf-8").strip() == "20260102T000000Z"


def test_meta_is_written_once_per_run(tmp_path: Path) -> None:
    # `make soak-meta` after a capture would otherwise drop the facts `start`
    # appended — the capture start and the firmware — and describe a container
    # that has since been replaced.
    soak = tmp_path / "soak"
    run = _run_dir(soak)
    run.mkdir(parents=True)
    (run / "META").write_text(
        "soak_run=20260101T000000Z\ncapture_started_utc=2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )

    refused = _run(["meta"], {"SOAK_SATELLITE": "0"}, soak)
    forced = _run(["meta"], {"SOAK_SATELLITE": "0", "SOAK_META_FORCE": "1"}, soak)

    assert refused.returncode == 1
    assert "already exists" in refused.stderr
    assert forced.returncode == 0, forced.stderr


def test_runs_lists_every_capture_newest_first(tmp_path: Path) -> None:
    soak = tmp_path / "soak"
    for name, firmware in (
        ("20260101T000000Z", "v0.4.0-25-g9adf84f"),
        ("20260102T000000Z", "v0.4.1"),
    ):
        (soak / name).mkdir(parents=True)
        (soak / name / "META").write_text(f"satellite_firmware={firmware}\n")
    (soak / "current").write_text("20260102T000000Z\n")

    result = _run(["runs"], {}, soak)

    assert result.returncode == 0, result.stderr
    lines = [line for line in result.stdout.splitlines() if line.startswith("  ")]
    assert "20260102T000000Z" in lines[0]
    assert "v0.4.1" in lines[0]
    assert "<- current" in lines[0]
    assert "v0.4.0-25-g9adf84f" in lines[1]


ESC = bytes([27])


def _serial_line(text: str) -> bytes:
    """Render one serial line the way `idf_monitor` writes it: colour + CRLF."""
    return ESC + b"[0;32m" + text.encode() + ESC + b"[0m\r\r\n"


def test_a_value_read_from_the_serial_log_is_plain_text(tmp_path: Path) -> None:
    # `idf_monitor` colourises every line and ends it with CRLF, so a naive sed
    # capture drags an ANSI reset and carriage returns into META. The escape is
    # invisible; the carriage return rewinds the terminal, which is how a run
    # listing came to print `<- current` over the run's own name — and a value
    # carrying an escape compares unequal to the same firmware read cleanly.
    log = tmp_path / "satellite-soak.log"
    log.write_bytes(
        _serial_line("I (630) app_init: App version:      v0.4.0-25-g9adf84f")
        + _serial_line("I (630) app_init: ESP-IDF:          v5.4.4")
    )

    firmware = _sourced("_log_value", str(log), "app_init: App version:")
    idf = _sourced("_log_value", str(log), "app_init: ESP-IDF:")

    assert firmware.stdout == "v0.4.0-25-g9adf84f\n"
    assert idf.stdout == "v5.4.4\n"


def test_runs_survives_a_meta_written_by_an_older_build(tmp_path: Path) -> None:
    soak = tmp_path / "soak"
    run = soak / "20260101T000000Z"
    run.mkdir(parents=True)
    # A META written before the escape-stripping fix.
    (run / "META").write_bytes(
        b"satellite_firmware=v0.4.0-25-g9adf84f" + ESC + b"[0m\r\r\n"
    )
    (soak / "current").write_text("20260101T000000Z\n")

    result = _run(["runs"], {}, soak)

    assert result.returncode == 0, result.stderr
    line = next(iter(result.stdout.splitlines()[1:]))
    assert "v0.4.0-25-g9adf84f" in line
    assert "\r" not in line and "\x1b" not in line
    assert "<- current" in line
    assert line.index("20260101T000000Z") == 2
