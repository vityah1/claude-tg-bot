#!/usr/bin/env bash
# Entry point for a fresh machine:
#
#   ./install.sh            install, or update an existing install
#   ./install.sh --doctor   diagnose an existing install, change nothing
#
# All this does is find a Python that can run bin/setup.py. It stays this
# small on purpose: the interactive part needs HTTP and JSON, and doing that
# in shell would mean depending on jq — one of the very things we are here
# to check for.
set -euo pipefail
cd "$(dirname "$0")"

find_python() {
    for candidate in python3 python3.14 python3.13 python3.12 python; do
        command -v "$candidate" >/dev/null 2>&1 || continue
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PY=$(find_python); then
    cat >&2 <<'MSG'
No usable Python found (3.8+ is needed to run the installer, 3.12+ for the bot).

Simplest fix — install uv, which downloads a matching Python itself and needs
no root:

    curl -LsSf https://astral.sh/uv/install.sh | sh

Then reopen the terminal and run ./install.sh again.
MSG
    exit 1
fi

exec "$PY" bin/setup.py "$@"
