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
# Usage:
#   soak_collectors.sh start     start both collectors (idempotent)
#   soak_collectors.sh stop      stop them by process group
#   soak_collectors.sh status    are they running, and following the right container?
#   soak_collectors.sh meta      print the META block for the current capture

set -euo pipefail

SOAK_DIR="${SOAK_DIR:-$HOME/wake-soak}"
BACKEND_CONTAINER="${BACKEND_CONTAINER:-taylored-assistant-prod-backend-1}"
FRONTEND_CONTAINER="${FRONTEND_CONTAINER:-taylored-assistant-prod-frontend-1}"

# The satellite sends the shared key as ?api_key= (the browser uses a
# subprotocol header instead), so every access log line contains it.
REDACT='s/\(api_key=\)[^ &"]*/\1REDACTED/g'

_pair() {
    # Print "log-name container-name" for each stream a soak records.
    printf '%s\n' "backend $BACKEND_CONTAINER" "frontend $FRONTEND_CONTAINER"
}

_container_id() {
    docker inspect -f '{{.Id}}' "$1" 2>/dev/null || true
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

cmd_start() {
    mkdir -p "$SOAK_DIR"
    while read -r name container; do
        local pgid_file="$SOAK_DIR/$name.pgid"
        local id_file="$SOAK_DIR/$name.container"
        if _running_pgid "$pgid_file" >/dev/null; then
            echo "  $name: already collecting (pgid $(cat "$pgid_file"))"
            continue
        fi
        if ! docker inspect "$container" >/dev/null 2>&1; then
            echo "  $name: no container named $container — is the stack up?" >&2
            return 1
        fi
        _container_id "$container" > "$id_file"
        # setsid => its own process group, so `stop` can kill the writer and the
        # `sed` filter together without touching this shell or the container.
        setsid nohup bash -c \
            "docker logs -t -f '$container' 2>&1 | sed -u '$REDACT' >> '$SOAK_DIR/$name.log'" \
            >/dev/null 2>&1 &
        local pid=$!
        # The group id is what stop needs; it equals the setsid pid because
        # setsid execs in place when it is not already a group leader.
        echo "$pid" > "$pgid_file"
        echo "  $name: collecting into $SOAK_DIR/$name.log (pgid $pid)"
    done < <(_pair)
}

cmd_stop() {
    local stopped=0
    while read -r name _container; do
        local pgid_file="$SOAK_DIR/$name.pgid"
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
        rm -f "$pgid_file" "$SOAK_DIR/$name.container"
    done < <(_pair)
    [ "$stopped" -eq 1 ] || echo "  nothing was running"
}

cmd_status() {
    while read -r name container; do
        local pgid_file="$SOAK_DIR/$name.pgid"
        local id_file="$SOAK_DIR/$name.container"
        if pgid="$(_running_pgid "$pgid_file")"; then
            local watched live
            watched="$(cat "$id_file" 2>/dev/null | cut -c1-12)"
            live="$(_container_id "$container" | cut -c1-12)"
            if [ "$watched" != "$live" ]; then
                # docker logs follows a container: a recreate ends the capture
                # while the process may still look alive for a moment.
                echo "  $name: FOLLOWING A DEAD CONTAINER ($watched != ${live:-none}) — restart the soak"
            else
                echo "  $name: collecting (pgid $pgid, container $live)"
            fi
        elif pgrep -f "docker logs -t -f $container" >/dev/null 2>&1; then
            # Started by hand (or by an older recipe): the capture is real, the
            # bookkeeping is not, so `stop` cannot kill it by group.
            echo "  $name: collecting UNMANAGED (no pgid file) — 'stop' will not end it"
        else
            echo "  $name: not collecting"
        fi
    done < <(_pair)
}

cmd_meta() {
    echo "captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "git_sha=$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if git diff --quiet 2>/dev/null; then echo "git_dirty=false"; else echo "git_dirty=true"; fi
    echo "app_version=${APP_VERSION:-unknown}"
    echo "backend_container=$(_container_id "$BACKEND_CONTAINER" | cut -c1-12)"
    echo "frontend_container=$(_container_id "$FRONTEND_CONTAINER" | cut -c1-12)"
    echo "backend_image=$(docker inspect -f '{{.Image}}' "$BACKEND_CONTAINER" 2>/dev/null | cut -c8-19 || echo none)"
    # The EFFECTIVE settings, read out of the running container: what a soak
    # actually ran with, not what a file says it should have.
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$BACKEND_CONTAINER" 2>/dev/null \
        | grep -E '^VOICE_' \
        | grep -viE '(API_KEY|SECRET|TOKEN|PASSWORD)=' \
        | sort || echo "voice_settings=unavailable"
}

case "${1:-}" in
    start) cmd_start ;;
    stop) cmd_stop ;;
    status) cmd_status ;;
    meta) cmd_meta ;;
    *)
        echo "usage: $(basename "$0") {start|stop|status|meta}" >&2
        exit 2
        ;;
esac
