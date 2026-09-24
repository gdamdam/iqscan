#!/bin/sh
set -eu
SCAN_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -x "$SCAN_DIR/.venv/bin/python" ]; then
    python3 -m venv "$SCAN_DIR/.venv"
fi
if ! "$SCAN_DIR/.venv/bin/python" -c 'import numpy, scipy, matplotlib' >/dev/null 2>&1; then
    "$SCAN_DIR/.venv/bin/python" -m pip install -r "$SCAN_DIR/requirements-scan.txt" >&2
fi
if [ "${1-}" = meteor ]; then
    for IQSCAN_OPTION do
        if [ "$IQSCAN_OPTION" = --video ] && ! "$SCAN_DIR/.venv/bin/python" -c 'import PIL, skyfield' >/dev/null 2>&1; then
            "$SCAN_DIR/.venv/bin/python" -m pip install -r "$SCAN_DIR/requirements-video.txt" >&2
            break
        fi
    done
fi
exec "$SCAN_DIR/.venv/bin/python" "$SCAN_DIR/iq_scan.py" "$@"
