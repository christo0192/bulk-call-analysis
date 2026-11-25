#!/bin/sh
set -eu

check() {
  command -v "$1" >/dev/null 2>&1 || { echo "Install $2 and put $1 on PATH." >&2; exit 1; }
}

check uv uv
check node "Node.js 20 or newer"
check npm npm
check cargo Rust
node_version=$(node --version)
node_major=${node_version#v}; node_major=${node_major%%.*}
case "$node_major" in ''|*[!0-9]*) echo "Install Node.js 20 or newer." >&2; exit 1;; esac
[ "$node_major" -ge 20 ] || { echo "Install Node.js 20 or newer." >&2; exit 1; }
printf '%s\n' "uv: found" "node: $node_version" "npm: found" "cargo: found"

repo=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
# spaCy is the optional nlp extra; --inexact keeps the name model, which is installed outside the lock.
(cd "$repo/engine" && uv sync --extra nlp --inexact) || { echo "uv sync failed; install uv and check the engine dependencies." >&2; exit 1; }
# Masking needs spaCy's small English model to find names; install the pinned wheel once.
(cd "$repo/engine" && uv run python -c "from bca.setup.spacy_model import ensure_model; ensure_model()") || { echo "Installing the name-masking model failed; check the internet connection and run this again." >&2; exit 1; }
if [ ! -d "$repo/shell/ui/node_modules" ] || [ "$repo/shell/ui/package-lock.json" -nt "$repo/shell/ui/node_modules" ]; then
  (cd "$repo/shell/ui" && npm ci) || { echo "npm ci failed; install Node.js and npm dependencies." >&2; exit 1; }
fi
(cd "$repo/shell/ui" && npm run build) || { echo "npm run build failed; fix the UI build before starting the app." >&2; exit 1; }
if [ "${1-}" = "--check" ]; then echo "Checks complete; the app was not started."; exit 0; fi

export BCA_ENGINE_PROGRAM="$repo/engine/.venv/bin/python"
export BCA_ENGINE_ARGS='["-m","bca.api.stdio"]'
cd "$repo/shell/src-tauri"
exec cargo run --release --features custom-protocol
