#!/usr/bin/env bash
# Launch the Scrapling Scraping API (separate from the dashboard UI).
set -e
PY="${SCRAPLING_PY:-/usr/bin/python3.10}"
cd "$(dirname "$0")"
exec "$PY" api_server.py
