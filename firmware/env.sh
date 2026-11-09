#!/usr/bin/env bash
# firmware/env.sh
#
# Load the ESP-IDF toolchain into the CURRENT shell.
#
# MUST be sourced, not executed:
#
#     source firmware/env.sh
#
# A script runs in a child process, so every export below dies with it and
# `idf.py` is "not found" afterwards. Sourcing runs it in your shell.
#
# To avoid typing that in every terminal, run this ONCE from the repo root — it writes the absolute
# path of this clone into ~/.bashrc, so it is correct wherever the repo lives:
#
#     echo "esp-idf() { . \"$PWD/firmware/env.sh\"; }" >> ~/.bashrc && exec bash
#
# then just run `esp-idf`.
#
# Every path can be overridden from the environment if a machine differs:
# ESP_IDF_DIR, ESP_IDF_PYTHON, ESP_IDF_BUILD_TOOLS.

# Resolve this script's directory whether it is sourced from bash or zsh.
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _env_sh_self="${BASH_SOURCE[0]}"
else
    _env_sh_self="$0"
fi
REPO_ROOT="$(cd "$(dirname "$_env_sh_self")/.." && pwd)"
unset _env_sh_self

ESP_IDF_DIR="${ESP_IDF_DIR:-$HOME/esp/esp-idf}"
ESP_IDF_PYTHON="${ESP_IDF_PYTHON:-$HOME/.local/share/esp-idf-python}"
ESP_IDF_BUILD_TOOLS="${ESP_IDF_BUILD_TOOLS:-$HOME/.local/share/idf-build-tools/bin}"

_env_sh_fail() {
    echo "env.sh: $1" >&2
    echo "        see $REPO_ROOT/docs/satellite.md §3" >&2
    return 1
}

# Already active: exporting twice would keep re-prepending to PATH.
if [ -n "${IDF_PATH:-}" ] && command -v idf.py >/dev/null 2>&1; then
    echo "env.sh: ESP-IDF already loaded (IDF_PATH=$IDF_PATH)"
    return 0
fi

[ -f "$ESP_IDF_DIR/export.sh" ] ||
    { _env_sh_fail "no ESP-IDF at $ESP_IDF_DIR (set ESP_IDF_DIR)"; return 1; }
[ -x "$ESP_IDF_PYTHON/python3" ] ||
    { _env_sh_fail "no python3 shim at $ESP_IDF_PYTHON (see §3 step 2)"; return 1; }
[ -d "$ESP_IDF_BUILD_TOOLS" ] ||
    { _env_sh_fail "no build tools at $ESP_IDF_BUILD_TOOLS (see §3 step 3)"; return 1; }

# Both directories must precede the system ones, in this order:
#   1. build tools — cmake and ninja, installed without root
#   2. python shim — a python3 that is the version ESP-IDF's venv was built with
# Prepending only when absent keeps repeated calls from growing PATH.
case ":$PATH:" in
    *":$ESP_IDF_BUILD_TOOLS:"*) ;;
    *) PATH="$ESP_IDF_BUILD_TOOLS:$PATH" ;;
esac
case ":$PATH:" in
    *":$ESP_IDF_PYTHON:"*) ;;
    *) PATH="$ESP_IDF_PYTHON:$PATH" ;;
esac
export PATH

# export.sh prepends the Xtensa compiler, the venv and idf.py itself, and sets
# IDF_PATH. The leading dot runs it in this shell.
# shellcheck disable=SC1091
. "$ESP_IDF_DIR/export.sh" || { _env_sh_fail "export.sh failed"; return 1; }

# Prove where each piece came from, so a wrong toolchain is obvious now instead
# of surfacing as a puzzling compiler error later.
echo "idf.py      : $(command -v idf.py)"
echo "xtensa gcc  : $(command -v xtensa-esp32s3-elf-gcc || echo MISSING)"
echo "python      : $(python3 --version 2>&1)"
echo "IDF version : $(cat "$ESP_IDF_DIR/version.txt" 2>/dev/null || git -C "$ESP_IDF_DIR" describe --tags 2>/dev/null)"
