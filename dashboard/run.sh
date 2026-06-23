#!/usr/bin/env bash
# Launch the Scrapling Dashboard. Uses the interpreter that has Scrapling's full
# stack installed (Python 3.10 on this machine).
set -e
PY="${SCRAPLING_PY:-/usr/bin/python3.10}"
cd "$(dirname "$0")"
exec "$PY" server.py
