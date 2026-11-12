#!/usr/bin/env bash
# backend/scripts/soak_collectors.sh
#
# Start, stop and inspect the log collectors a soak needs.
#
# Called by the Makefile (`make soak-up`, `soak-stop`, `soak-status`); run it
# directly only when debugging one of those.
#
# Why a helper instead of a few Makefile lines: a collector has to survive the
# shell that started it, follow a CONTAINER (not a service), and be killable
# later by something that never saw the original pid. The first soak got this
# wrong twice — `$!` pointed at the `setsid` wrapper while the writer was its
# child, and `docker logs -f` silently ended when a container was recreated,
# leaving two days of report looking like a quiet house.
#
# Three streams, of two kinds:
#
#   backend, frontend   kind=container   `docker logs -t -f` piped through the
#                                        api_key redactor
#   satellite           kind=serial      `idf.py monitor` inside a pty provided
#                                        by `script`, fed by a FIFO
#
# The satellite cannot follow the container recipe. `screen`, the documented
# way for a HUMAN to watch the device, refuses to run without a TTY — and
# behind `>/dev/null 2>&1` that failure is invisible, leaving a zero-byte
# capture. `script` allocates its own pty, so it works from `make`, from cron
# and from an agent. Three of its defaults then matter, and each was measured:
#
#   - without `-f` it buffers the whole typescript and writes it at exit;
#   - with stdin at /dev/null the child sees EOF at once and dies;
#   - without `-a` it truncates an existing capture.
#
# So: `-f` always, a FIFO feeder for stdin, and `-a` only when appending.
#
# Requires bash >= 4.4 for empty-array expansion; Debian and Git Bash both ship
# 5.x, and the container streams are unaffected either way.
#
# Usage:
#   soak_collectors.sh plan        print the resolved streams; change nothing
#   soak_collectors.sh runs        list the captures in the soak directory
#   soak_collectors.sh stack-state soak | preexisting — who owns the Docker stack
#   soak_collectors.sh preflight   check everything a start would need
#   soak_collectors.sh start       start the collectors (idempotent)
#   soak_collectors.sh stop        stop them by process group
#   soak_collectors.sh status      are they running, and is the capture alive?
#   soak_collectors.sh meta        write META (the only writer of that file)
#   soak_collectors.sh meta append add what the running capture proved

set -euo pipefail

SOAK_DIR="${SOAK_DIR:-$HOME/wake-soak}"
SOAK_REPO="${SOAK_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-taylored-assistant-prod-backend-1}"
FRONTEND_CONTAINER="${FRONTEND_CONTAINER:-taylored-assistant-prod-frontend-1}"

# Every knob shares one prefix, because `SATELLITE` beside `SOAK_APPEND` is how
# a README ends up documenting a variable the script does not read.
SOAK_SATELLITE="${SOAK_SATELLITE:-1}"
SOAK_SATELLITE_PORT="${SOAK_SATELLITE_PORT:-}"
SOAK_APPEND="${SOAK_APPEND:-0}"
SOAK_FIRST_LINE_S="${SOAK_FIRST_LINE_S:-20}"
SOAK_SATELLITE_S="${SOAK_SATELLITE_S:-120}"
SOAK_STALE_S="${SOAK_STALE_S:-300}"
# Where the stable per-device links live. Overridable so the discovery logic can
# be tested without a board plugged in.
SOAK_SERIAL_DIR="${SOAK_SERIAL_DIR:-/dev/serial/by-id}"

# Every capture lives in its own directory named after the moment it started, so
# two soaks can never overwrite, interleave or append to one another — and the
# name is the label that relates a log to the run it came from. Two run
# directories side by side are self-explanatory; `backend.log` written twice
# is not.
#
# `current` points at the run the other subcommands act on, so `stop`, `status`
# and `report` work from any shell without carrying an environment variable.
SOAK_RUN_ID="${SOAK_RUN_ID:-}"
# Who brought the Docker stack up. `soak-down` undoes the soak, which includes
# the stack — unless the stack was already running before, in which case taking
# it down would stop something the operator started and did not ask us to stop.
STACK_OWNER_FILE="$SOAK_DIR/stack.owner"
SOAK_RUN_DIR=""          # resolved by _resolve_run
PLAN_FILE="$SOAK_DIR/.no-run/plan"   # replaced by _resolve_run
CURRENT_POINTER="$SOAK_DIR/current"

# The satellite sends the shared key as ?api_key= (the browser uses a
# subprotocol header instead), so every access log line contains it.
REDACT='s/\(api_key=\)[^ &"]*/\1REDACTED/g'

_stack_running() {
    # True when any container of the prod compose project is running.
    docker ps -q \
        --filter label=com.docker.compose.project=taylored-assistant-prod \
        2>/dev/null | grep -q .
}

_stack_owner() {
    # Decide, and remember, who owns the stack.
    #
    #   soak         we are about to start it (nothing was running), or a
    #                previous soak left it running — either way it is ours
    #   preexisting  it was already up and no soak marker says otherwise, so it
    #                belongs to the operator: `soak-down` must not stop it
    # A marker only means something while a stack is actually up: with nothing
    # running, whatever it says is history, and the stack the next soak starts
    # is that soak's.
    if _stack_running; then
        if [ -f "$STACK_OWNER_FILE" ]; then
            cat "$STACK_OWNER_FILE"
        else
            printf '%s\n' "preexisting"
        fi
        return 0
    fi
    printf '%s\n' "soak"
}

_new_run_id() {
    # A UTC timestamp, because it sorts, it needs no lookup table, and it says
    # when the soak started without opening a file.
    date -u +%Y%m%dT%H%M%SZ
}

_resolve_run() {
    # Decide which run directory the command acts on.
    #
    #   new      a capture is about to start: honour a pinned SOAK_RUN_ID, else
    #            mint a fresh stamp. It must NOT reuse the pointer, or starting
    #            a second soak would write into the first one's directory.
    #   current  act on the capture the pointer names (default): stop, status,
    #            meta and the report all mean "the one that just ran".
    #
    # Only these two modes exist, so a read-only command never creates an empty
    # directory and a new soak never inherits an old name.
    local mode="${1:-current}" run_id
    if [ -n "$SOAK_RUN_ID" ]; then
        run_id="$SOAK_RUN_ID"
    elif [ "$mode" = new ]; then
        run_id="$(_new_run_id)"
    elif [ -f "$CURRENT_POINTER" ]; then
        run_id="$(cat "$CURRENT_POINTER")"
    else
        run_id=""
    fi
    SOAK_RUN_ID="$run_id"
    if [ -n "$run_id" ]; then
        SOAK_RUN_DIR="$SOAK_DIR/$run_id"
        PLAN_FILE="$SOAK_RUN_DIR/plan"
    else
        SOAK_RUN_DIR=""
        # Reads of a file that cannot exist must not collapse to "/plan".
        PLAN_FILE="$SOAK_DIR/.no-run/plan"
    fi
}

_require_run() {
    # Refuse to guess which capture a command means.
    _resolve_run
    if [ -z "$SOAK_RUN_DIR" ]; then
        echo "no soak run found in $SOAK_DIR — start one with 'make soak-up'" >&2
        return 1
    fi
    if [ ! -d "$SOAK_RUN_DIR" ]; then
        echo "soak run ${SOAK_RUN_ID} does not exist in $SOAK_DIR" >&2
        return 1
    fi
}

_log_path() {
    # The satellite's file has a name of its own; the report looks for it by
    # that name inside the run directory.
    local base="${SOAK_RUN_DIR:-$SOAK_DIR/.no-run}"
    case "$1" in
        satellite) printf '%s\n' "$base/satellite-soak.log" ;;
        *) printf '%s\n' "$base/$1.log" ;;
    esac
}

_pgid_path() { printf '%s\n' "$SOAK_RUN_DIR/$1.pgid"; }

_satellite_port() {
    # Resolve the serial port, printing it on success.
    #
    # Refuses rather than guessing: two serial devices on one host is a question
    # only the operator can answer, and attributing a soak to the wrong board is
    # worse than not starting.
    if [ -n "$SOAK_SATELLITE_PORT" ]; then
        printf '%s\n' "$SOAK_SATELLITE_PORT"
        return 0
    fi
    local candidates=() seen=" " path real
    for path in "$SOAK_SERIAL_DIR"/*; do
        [ -e "$path" ] || continue
        real="$(readlink -f "$path")"
        case "$seen" in
            *" $real "*) continue ;;
        esac
        seen="$seen$real "
        candidates+=("$path")
    done
    if [ "${#candidates[@]}" -eq 0 ]; then
        echo "satellite: no $SOAK_SERIAL_DIR device — is the board plugged in?" >&2
        return 1
    fi
    if [ "${#candidates[@]}" -gt 1 ]; then
        echo "satellite: more than one serial device, refusing to guess:" >&2
        printf '  %s\n' "${candidates[@]}" >&2
        echo "satellite: choose one with SOAK_SATELLITE_PORT=/dev/serial/by-id/..." >&2
        return 1
    fi
    printf '%s\n' "${candidates[0]}"
}

_streams() {
    # Print "name kind target" for each stream this run should collect.
    #
    # The satellite entry survives an unplugged board: `stop` and `status` still
    # have to reach a group that is already running, and the recorded port is
    # what tells them which one it was.
    printf '%s\n' "backend container $BACKEND_CONTAINER"
    printf '%s\n' "frontend container $FRONTEND_CONTAINER"
    [ "$SOAK_SATELLITE" = 1 ] || return 0
    local port
    if port="$(_satellite_port 2>/dev/null)"; then
        printf '%s\n' "satellite serial $port"
        return 0
    fi
    port="$(sed -n 's/^satellite_port=//p' "$PLAN_FILE" 2>/dev/null | tail -1 || true)"
    printf '%s\n' "satellite serial ${port:-unknown}"
}

_container_id() {
    docker inspect -f '{{.Id}}' "$1" 2>/dev/null || true
}

_port_holder() {
    # Print the pid holding a serial port, if any.
    #
    # A group-scoped probe cannot see a leftover manual `screen`/`idf.py` from
    # an earlier session, and that failure surfaces minutes later as "no boot
    # banner" — which reads like a device fault.
    local port="$1" holder=""
    if command -v fuser >/dev/null 2>&1; then
        holder="$(fuser "$port" 2>/dev/null | tr -s ' ' '\n' | sed '/^$/d' | head -1 || true)"
    fi
    if [ -z "$holder" ]; then
        holder="$(pgrep -f "idf.py -p $port" 2>/dev/null | head -1 || true)"
    fi
    [ -n "$holder" ] || return 1
    printf '%s\n' "$holder"
}

_running_pgid() {
    local pidfile="$1"
    [ -f "$pidfile" ] || return 1
    local pgid
    pgid="$(cat "$pidfile")"
    [ -n "$pgid" ] || return 1
    # A recorded group is alive when the negative-pid probe succeeds.
    kill -0 -- "-$pgid" 2>/dev/null || return 1
    printf '%s\n' "$pgid"
}

_log_value() {
    # Read the last value of a boot-banner field from the serial log.
    #
    # The log is not plain text: `idf_monitor` colourises each line and ends it
    # with CRLF, so a naive `sed` capture drags an ANSI reset and two carriage
    # returns into META. Those are invisible here and lethal later — a carriage
    # return rewinds the terminal (which is how `<- current` came to overwrite
    # the run name) and a value with a trailing escape compares unequal to the
    # same firmware read from a clean file.
    local log="$1" pattern="$2"
    # Cut at the first control character rather than deleting control
    # characters one by one: deleting the ESC alone would leave the bracket
    # sequence behind as literal text.
    sed -n "s/.*$pattern *//p" "$log" 2>/dev/null \
        | tail -1 \
        | sed -e 's/[[:cntrl:]].*//' \
        | sed -e 's/[[:space:]]*$//'
}

_log_age_s() {
    local file="$1"
    [ -f "$file" ] || return 1
    local now modified
    now="$(date +%s)"
    modified="$(stat -c %Y "$file" 2>/dev/null || echo "$now")"
    echo $((now - modified))
}

_only_script_noise() {
    # True when a log holds nothing but `script`'s own header, trailer and blank
    # lines — i.e. a start that never produced firmware output.
    #
    # Anything else is evidence and is kept: a failed cmake reconfigure prints
    # ~110 lines and not one of them is a firmware line, so a "no `I (`" test
    # would delete exactly the output the operator needs.
    [ -f "$1" ] || return 1
    if grep -qvE '^(Script (started|done) on.*)?$' "$1"; then
        return 1
    fi
    return 0
}

_guard_append() {
    # Refuse to start a stream over an existing capture.
    #
    # `script` truncates and `>>` appends, so without this a second soak either
    # wipes the satellite capture or interleaves the container ones. Moving the
    # old file aside is the operator's decision, not this script's.
    local name="$1" file="$2"
    if [ "$SOAK_APPEND" = 1 ]; then
        return 0
    fi
    if [ -s "$file" ]; then
        echo "  $name: $file already has bytes — move it aside, or set SOAK_APPEND=1" >&2
        return 1
    fi
}

_wait_for() {
    # Poll a file for a pattern, up to a deadline.
    local file="$1" pattern="$2" seconds="$3" waited=0
    while [ "$waited" -lt "$seconds" ]; do
        if [ -f "$file" ] && grep -qE "$pattern" "$file" 2>/dev/null; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

_tail_of() {
    local file="$1"
    [ -f "$file" ] || return 0
    echo "  --- last lines of $file ---" >&2
    tail -n 8 "$file" | sed 's/^/  /' >&2 || true
    if [ -f "$file.err" ]; then
        echo "  --- $file.err ---" >&2
        tail -n 8 "$file.err" | sed 's/^/  /' >&2 || true
    fi
}

cmd_plan() {
    _resolve_run
    if [ "$SOAK_SATELLITE" != 1 ]; then
        echo "satellite: skipped (SOAK_SATELLITE=$SOAK_SATELLITE)"
    else
        local port
        if ! port="$(_satellite_port)"; then
            echo "satellite: unavailable (no port resolved)"
        else
            echo "satellite: port $port (device $(readlink -f "$port"))"
            echo "satellite: append=$SOAK_APPEND log=$(_log_path satellite)"
            local flag=""
            if [ "$SOAK_APPEND" = 1 ]; then
                flag=" -a"
            fi
            echo "satellite: argv script -f -q$flag -c \"idf.py -p $port monitor\" $(_log_path satellite)"
        fi
    fi
    echo "streams:"
    _streams | sed 's/^/  /'
}

cmd_preflight() {
    _resolve_run new
    # Checks only: nothing is built, nothing is started, no toolchain is loaded.
    # This runs BEFORE `docker compose build`, so a missing device costs a
    # second instead of a full image build.
    local failed=0 plan="" port=""

    if docker ps --format '{{.Names}}' | grep -q '^taylored-assistant-dev-'; then
        echo "preflight: the dev stack is running — 'make dev-down' first." >&2
        echo "           Both stacks publish langfuse-web on :3000." >&2
        failed=1
    fi
    if [ ! -f "$SOAK_REPO/infra/compose/.env.prod" ]; then
        echo "preflight: missing infra/compose/.env.prod (copy .env.prod.example)" >&2
        failed=1
    fi

    mkdir -p "$SOAK_DIR"
    local owner

    # The previous run is checked before the new one: its collectors are still
    # alive in their own directory, and two captures at once would each report
    # half a conversation.
    if [ -f "$CURRENT_POINTER" ]; then
        local previous previous_dir
        previous="$(cat "$CURRENT_POINTER")"
        previous_dir="$SOAK_DIR/$previous"
        if [ -d "$previous_dir" ]; then
            local busy
            for busy in "$previous_dir"/*.pgid; do
                [ -e "$busy" ] || continue
                if _running_pgid "$busy" >/dev/null; then
                    echo "preflight: run $previous is still collecting — 'make soak-stop' first" >&2
                    failed=1
                    break
                fi
            done
        fi
    fi

    local name kind target
    while read -r name kind target; do
        if [ "$kind" = serial ]; then
            port="$target"
            continue
        fi
        if _running_pgid "$SOAK_RUN_DIR/$name.pgid" >/dev/null; then
            echo "preflight: $name is already collecting (pgid $(cat "$SOAK_RUN_DIR/$name.pgid"))" >&2
            failed=1
        fi
        _guard_append "$name" "$(_log_path "$name")" || failed=1
    done < <(_streams)

    plan="satellite=$SOAK_SATELLITE"
    if [ "$SOAK_SATELLITE" != 1 ]; then
        plan="$plan
satellite_skipped=SOAK_SATELLITE=$SOAK_SATELLITE"
    else
        if [ -z "$port" ] || [ "$port" = unknown ]; then
            echo "preflight: no satellite port — plug the board in, or run" >&2
            echo "           'make soak-up SOAK_SATELLITE=0' for a server-only soak" >&2
            failed=1
        else
            if [ ! -r "$port" ]; then
                echo "preflight: $port is not readable." >&2
                echo "           sudo usermod -aG dialout \"\$USER\"; newgrp dialout" >&2
                echo "           A newly opened terminal will NOT help: groups are" >&2
                echo "           fixed when a process starts." >&2
                failed=1
            fi
            local holder
            if holder="$(_port_holder "$port")"; then
                echo "preflight: $port is held by pid $holder — stop it first" >&2
                echo "           (a leftover 'idf.py monitor' or 'screen' ends a capture)" >&2
                failed=1
            fi
            if _running_pgid "$SOAK_RUN_DIR/satellite.pgid" >/dev/null; then
                echo "preflight: satellite is already collecting (pgid $(cat "$SOAK_RUN_DIR/satellite.pgid"))" >&2
                failed=1
            fi
            _guard_append satellite "$(_log_path satellite)" || failed=1
            plan="$plan
satellite_port=$port
satellite_device=$(readlink -f "$port")"
        fi
    fi

    if [ "$failed" -ne 0 ]; then
        return 1
    fi
    owner="$(_stack_owner)"
    plan="$plan
stack_owner=$owner"
    mkdir -p "$SOAK_RUN_DIR"
    printf '%s\n' "$plan" > "$PLAN_FILE"
    # The pointer is written only on success, so a refused start never leaves a
    # run id that names an empty directory.
    printf '%s\n' "$SOAK_RUN_ID" > "$CURRENT_POINTER"
    printf '%s\n' "$owner" > "$STACK_OWNER_FILE"
    echo "preflight: ok (run $SOAK_RUN_ID, plan written to $SOAK_RUN_DIR/plan)"
    if [ "$owner" = preexisting ]; then
        echo "preflight: the stack is already running and was not started by a soak;"
        echo "           make soak-down will leave it alone (make prod-down stops it)"
    fi
}

_start_container() {
    local name="$1" container="$2"
    local pgid_file="$SOAK_RUN_DIR/$name.pgid"
    local log
    log="$(_log_path "$name")"
    if _running_pgid "$pgid_file" >/dev/null; then
        echo "  $name: already collecting (pgid $(cat "$pgid_file"))"
        return 0
    fi
    # Reached without a preflight (by hand, or after a stop): the run it
    # resolved may already hold a capture, which `>>` would interleave.
    _guard_append "$name" "$log" || return 1
    if ! docker inspect "$container" >/dev/null 2>&1; then
        echo "  $name: no container named $container — is the stack up?" >&2
        return 1
    fi
    _container_id "$container" > "$SOAK_RUN_DIR/$name.container"
    # setsid => its own process group, so `stop` can kill the writer and the
    # `sed` filter together without touching this shell or the container.
    setsid nohup bash -c \
        "docker logs -t -f '$container' 2>&1 | sed -u '$REDACT' >> '$log'" \
        >/dev/null 2>&1 &
    local pid=$!
    # The group id is what stop needs; it equals the setsid pid because setsid
    # execs in place when it is not already a group leader.
    echo "$pid" > "$pgid_file"
    echo "  $name: collecting into $log (pgid $pid)"
    if ! _wait_for "$log" '.' "$SOAK_FIRST_LINE_S"; then
        echo "  $name: no line within ${SOAK_FIRST_LINE_S}s" >&2
        _tail_of "$log"
        return 1
    fi
}

_start_satellite() {
    local port="$1"
    local log
    log="$(_log_path satellite)"
    local pgid_file="$SOAK_RUN_DIR/satellite.pgid"
    if _running_pgid "$pgid_file" >/dev/null; then
        echo "  satellite: already collecting (pgid $(cat "$pgid_file"))"
        return 0
    fi
    _guard_append satellite "$log" || return 1
    rm -f "$log.exit"
    local started
    started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    # Values travel through the environment: splicing "$port" into a
    # single-quoted body is one quoting change away from a broken command.
    SOAK_REPO="$SOAK_REPO" SOAK_PORT="$port" SOAK_LOG="$log" SOAK_APPEND="$SOAK_APPEND" \
    LC_ALL=C PYTHONIOENCODING=utf-8 \
    setsid bash -c '
        set -u
        . "$SOAK_REPO/firmware/env.sh" >/dev/null 2>>"$SOAK_LOG.err" || exit 78
        cd "$SOAK_REPO/firmware/satellite-xvf3800-xiao" || exit 79
        work=$(mktemp -d) || exit 80
        trap '"'"'rm -rf "$work"'"'"' EXIT
        mkfifo "$work/in" || exit 81
        sleep infinity > "$work/in" & feeder=$!
        if [ "$SOAK_APPEND" = 1 ]; then
            script -f -q -a -c "idf.py -p $SOAK_PORT monitor" "$SOAK_LOG" < "$work/in"
        else
            script -f -q    -c "idf.py -p $SOAK_PORT monitor" "$SOAK_LOG" < "$work/in"
        fi
        echo $? > "$SOAK_LOG.exit"
        kill "$feeder" 2>/dev/null
    ' >/dev/null 2>&1 &
    local pid=$!
    echo "$pid" > "$pgid_file"
    echo "  satellite: collecting $port into $log (pgid $pid)"

    if ! _wait_for "$log" 'app_init: App version:' "$SOAK_SATELLITE_S"; then
        echo "  satellite: no boot banner within ${SOAK_SATELLITE_S}s" >&2
        _fail_start_satellite "$log"
        return 1
    fi
    if ! _wait_for "$log" 'ws: connected' "$SOAK_SATELLITE_S"; then
        echo "  satellite: booted but never reached the server within ${SOAK_SATELLITE_S}s" >&2
        _fail_start_satellite "$log"
        return 1
    fi
    echo "  satellite: booted and connected"
    cmd_meta append "$started"
}

_fail_start_satellite() {
    local log="$1"
    local pgid
    if pgid="$(_running_pgid "$SOAK_RUN_DIR/satellite.pgid")"; then
        kill -TERM -- "-$pgid" 2>/dev/null || true
        sleep 1
        kill -KILL -- "-$pgid" 2>/dev/null || true
    fi
    rm -f "$SOAK_RUN_DIR/satellite.pgid" "$log.exit"
    _tail_of "$log"
    if _only_script_noise "$log"; then
        echo "  satellite: removed $log (it held only script's header)" >&2
        rm -f "$log"
    fi
}

cmd_start() {
    _resolve_run current
    mkdir -p "$SOAK_RUN_DIR"
    # `start` is also reachable by hand, so it publishes the pointer too.
    printf '%s\n' "$SOAK_RUN_ID" > "$CURRENT_POINTER"
    local name kind target port="" satellite_failed=0
    while read -r name kind target; do
        if [ "$kind" = serial ]; then
            port="$target"
            continue
        fi
        _start_container "$name" "$target" || return 1
    done < <(_streams)
    if [ "$SOAK_SATELLITE" = 1 ]; then
        if [ "$port" = unknown ] || [ -z "$port" ]; then
            echo "  satellite: no port resolved — run 'make soak-preflight'" >&2
            satellite_failed=1
        else
            _start_satellite "$port" || satellite_failed=1
        fi
    else
        echo "  satellite: skipped (SOAK_SATELLITE=$SOAK_SATELLITE)"
        echo "             later: make soak-report UNTIL=<ISO> SINCE=<ISO>"
    fi
    [ "$satellite_failed" -eq 0 ] || return 1
}

cmd_stack_state() {
    # Print who owns the stack, for `make soak-down` to act on.
    printf '%s\n' "$(_stack_owner)"
}

cmd_stop() {
    _require_run || return 1
    local stopped=0
    while read -r name _kind _target; do
        local pgid_file="$SOAK_RUN_DIR/$name.pgid"
        if pgid="$( _running_pgid "$pgid_file" )"; then
            kill -TERM -- "-$pgid" 2>/dev/null || true
            # Give sed a moment to flush the last line, then insist.
            for _ in 1 2 3 4 5; do
                kill -0 -- "-$pgid" 2>/dev/null || break
                sleep 0.2
            done
            kill -KILL -- "-$pgid" 2>/dev/null || true
            echo "  $name: stopped (pgid $pgid)"
            stopped=1
        else
            echo "  $name: not collecting"
        fi
        rm -f "$pgid_file" "$SOAK_RUN_DIR/$name.container"
    done < <(_streams)
    # Per-capture side files go too, so a later `make soak-meta` cannot fold in
    # a previous soak's device facts.
    rm -f "$PLAN_FILE" "$(_log_path satellite).exit"
    [ "$stopped" -eq 1 ] || echo "  nothing was running"
}

cmd_status() {
    _require_run || return 1
    echo "run      : $SOAK_RUN_ID ($SOAK_RUN_DIR)"
    while read -r name kind target; do
        local pgid_file="$SOAK_RUN_DIR/$name.pgid"
        local log
        log="$(_log_path "$name")"
        if pgid="$(_running_pgid "$pgid_file")"; then
            if [ "$kind" = container ]; then
                local watched live
                watched="$(cat "$SOAK_RUN_DIR/$name.container" 2>/dev/null | cut -c1-12)"
                live="$(_container_id "$target" | cut -c1-12)"
                if [ "$watched" != "$live" ]; then
                    # docker logs follows a container: a recreate ends the
                    # capture while the process may still look alive.
                    echo "  $name: FOLLOWING A DEAD CONTAINER ($watched != ${live:-none}) — restart the soak"
                else
                    echo "  $name: collecting (pgid $pgid, container $live)"
                fi
                continue
            fi
            if [ ! -e "$target" ]; then
                echo "  $name: DEVICE GONE ($target removed while collecting)"
            elif ! pgrep -g "$pgid" -x script >/dev/null 2>&1; then
                echo "  $name: capture process gone (pgid $pgid) — not collecting"
            elif [ -f "$log.exit" ]; then
                echo "  $name: monitor exited (rc=$(cat "$log.exit"))"
            else
                local age
                age="$(_log_age_s "$log" || echo 0)"
                if [ "$age" -gt "$SOAK_STALE_S" ]; then
                    echo "  $name: SILENT (log idle ${age}s)"
                else
                    echo "  $name: collecting (pgid $pgid, log ${age}s ago)"
                fi
            fi
        elif [ "$kind" = container ] && pgrep -f "docker logs -t -f $target" >/dev/null 2>&1; then
            # Started by hand (or by an older recipe): the capture is real, the
            # bookkeeping is not, so `stop` cannot kill it by group.
            echo "  $name: collecting UNMANAGED (no pgid file) — 'stop' will not end it"
        elif [ "$kind" = serial ] && [ "$target" != unknown ] \
            && pgrep -f "idf.py -p $target monitor" >/dev/null 2>&1; then
            echo "  $name: collecting UNMANAGED (no pgid file) — 'stop' will not end it"
        else
            echo "  $name: not collecting"
        fi
    done < <(_streams)
}

cmd_runs() {
    # List the captures in the soak directory, newest first.
    #
    # A run is a directory named after the moment it started, so the answer to
    # "which soak is this log from" is the path itself.
    local dir name current="" firmware
    mkdir -p "$SOAK_DIR"
    if [ -f "$CURRENT_POINTER" ]; then
        current="$(cat "$CURRENT_POINTER")"
    fi
    if ! ls -d "$SOAK_DIR"/*/ >/dev/null 2>&1; then
        echo "no runs in $SOAK_DIR"
        return 0
    fi
    echo "runs in $SOAK_DIR (newest first):"
    while read -r dir; do
        name="$(basename "$dir")"
        firmware="no firmware recorded"
        if [ -f "$dir/META" ]; then
            firmware="$(sed -n 's/^satellite_firmware=//p' "$dir/META" | tail -1 | sed -e 's/[[:cntrl:]].*//')"
            if [ -z "$firmware" ]; then firmware="no satellite"; fi
        fi
        local marker=""
        if [ "$name" = "$current" ]; then marker="  <- current"; fi
        printf '  %-22s %8s  %-22s%s\n' \
            "$name" "$(du -sh "$dir" 2>/dev/null | cut -f1)" "$firmware" "$marker" \
            | sed -e 's/[[:space:]]*$//'
    done < <(ls -d "$SOAK_DIR"/*/ 2>/dev/null | sort -r)
}

cmd_meta() {
    _require_run || return 1
    local mode="${1:-write}" started="${2:-}"
    if [ "$mode" != append ]; then
        # META describes one run and is written once, by `soak-up`, after the
        # stack is up. Rewriting it later would drop the facts `start` appended
        # (the capture start and the firmware) — and while a capture runs it
        # would also describe a container that has since been replaced.
        if [ -s "$SOAK_RUN_DIR/META" ] && [ "${SOAK_META_FORCE:-0}" != 1 ]; then
            echo "meta: $SOAK_RUN_DIR/META already exists for run $SOAK_RUN_ID" >&2
            echo "      it is written once per run — 'meta append' adds to it," >&2
            echo "      SOAK_META_FORCE=1 regenerates it" >&2
            return 1
        fi
        local name kind target
        while read -r name kind target; do
            if _running_pgid "$SOAK_RUN_DIR/$name.pgid" >/dev/null; then
                echo "meta: $name is collecting — use 'meta append'" >&2
                return 1
            fi
        done < <(_streams)
        mkdir -p "$SOAK_DIR"
        {
            echo "soak_run=$SOAK_RUN_ID"
            echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
            echo "git_sha=$(git -C "$SOAK_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
            if git -C "$SOAK_REPO" diff --quiet 2>/dev/null; then
                echo "git_dirty=false"
            else
                echo "git_dirty=true"
            fi
            echo "app_version=${APP_VERSION:-unknown}"
            echo "backend_container=$(_container_id "$BACKEND_CONTAINER" | cut -c1-12)"
            echo "frontend_container=$(_container_id "$FRONTEND_CONTAINER" | cut -c1-12)"
            echo "backend_image=$(docker inspect -f '{{.Image}}' "$BACKEND_CONTAINER" 2>/dev/null | cut -c8-19 || echo none)"
            # The EFFECTIVE settings, read out of the running container: what a
            # soak actually ran with, not what a file says it should have.
            docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$BACKEND_CONTAINER" 2>/dev/null \
                | grep -E '^VOICE_' \
                | grep -viE '(API_KEY|SECRET|TOKEN|PASSWORD)=' \
                | sort || echo "voice_settings=unavailable"
            if [ -s "$PLAN_FILE" ]; then
                cat "$PLAN_FILE"
            fi
        } > "$SOAK_RUN_DIR/META"
        return 0
    fi

    # Append what the running capture proved. Called only when this run actually
    # started the satellite, so a duplicate start cannot duplicate keys.
    local log
    log="$(_log_path satellite)"
    {
        echo "capture_started_utc=${started:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
        local firmware idf
        firmware="$(_log_value "$log" 'app_init: App version:')"
        idf="$(_log_value "$log" 'app_init: ESP-IDF:')"
        if [ -n "$firmware" ]; then echo "satellite_firmware=$firmware"; fi
        if [ -n "$idf" ]; then echo "satellite_idf=$idf"; fi
    } >> "$SOAK_RUN_DIR/META"
}

# Dispatched only when executed, so the helpers above can be sourced by a test
# without a subcommand.
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    case "${1:-}" in
        plan) cmd_plan ;;
        runs) cmd_runs ;;
        stack-state) cmd_stack_state ;;
        preflight) cmd_preflight ;;
        start) cmd_start ;;
        stop) cmd_stop ;;
        status) cmd_status ;;
        meta) shift; cmd_meta "${1:-write}" "${2:-}" ;;
        *)
            echo "usage: $(basename "$0") {plan|runs|stack-state|preflight|start|stop|status|meta [append]}" >&2
            exit 2
            ;;
    esac
fi
