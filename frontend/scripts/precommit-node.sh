#!/usr/bin/env bash
# frontend/scripts/precommit-node.sh
# Run a frontend Node tool (eslint, prettier) from a pre-commit git hook.
#
# WHY THIS EXISTS: `git commit` runs hooks with a minimal, non-interactive
# environment. Shell rc files are never sourced, so a Node installed by nvm —
# or any per-user prefix like ~/.local/bin — is invisible and the hook dies
# with "npx: command not found" (exit 127) even though npx works in the
# terminal. Resolving Node here keeps the hooks working regardless of how the
# developer installed it, and replaces the bare 127 with an actionable error.
#
# Usage: precommit-node.sh <tool> [args...]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root/frontend"

nvm_versions="${NVM_DIR:-$HOME/.nvm}/versions/node"

# Candidate bin dirs, most specific first: the version pinned by .nvmrc, the
# newest nvm install, then common per-user/system prefixes.
candidates=()
if [ -r .nvmrc ]; then
    pinned="$(tr -d '[:space:]' < .nvmrc)"
    candidates+=("$nvm_versions/v$pinned/bin")
fi
if [ -d "$nvm_versions" ]; then
    newest="$(ls -1d "$nvm_versions"/v* 2>/dev/null | sort -V | tail -1 || true)"
    [ -n "$newest" ] && candidates+=("$newest/bin")
fi
candidates+=("$HOME/.local/bin" "/usr/local/bin" "/usr/bin")

if ! command -v npx >/dev/null 2>&1; then
    for bin in "${candidates[@]}"; do
        if [ -x "$bin/npx" ]; then
            export PATH="$bin:$PATH"
            break
        fi
    done
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "frontend hook: npx not found." >&2
    echo "Install Node 24.19.0 (README Step 7a), e.g. 'nvm install 24.19.0'," >&2
    echo "or put its bin directory on PATH and commit again." >&2
    exit 1
fi

if [ ! -d node_modules ]; then
    echo "frontend hook: frontend/node_modules is missing." >&2
    echo "Run 'cd frontend && npm ci' and commit again." >&2
    exit 1
fi

exec npx "$@"
