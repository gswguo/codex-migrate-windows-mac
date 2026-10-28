#!/bin/sh
# Source launcher: no downloads, installation, or changes to Codex data.
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd) || exit 1
PYTHON_BIN=
if [ -n "${CODEX_MIGRATE_PYTHON:-}" ]; then
    if "$CODEX_MIGRATE_PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
        PYTHON_BIN=$CODEX_MIGRATE_PYTHON
    fi
else
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN=$candidate
            break
        fi
    done
fi
if [ -z "$PYTHON_BIN" ]; then
    printf '%s\n' 'Python 3.11 or newer is required. Install Python yourself, or set CODEX_MIGRATE_PYTHON to its executable.' 'Nothing was installed and no Codex data was changed.'
    if [ -t 0 ]; then
        printf '%s' 'Press Return to close: '
        read -r answer
    fi
    exit 1
fi
PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
if [ "$#" -eq 0 ]; then
    set -- wizard
fi
exec "$PYTHON_BIN" -m codex_migrate "$@"
