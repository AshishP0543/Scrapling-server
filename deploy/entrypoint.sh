#!/usr/bin/env bash
# Pod entrypoint. Picks which service(s) to run inside the container.
#   SERVICE=api        → run only api_server.py (default)
#   SERVICE=dashboard  → run only server.py (UI)
#   SERVICE=both       → run both, forward signals, exit if either dies
set -euo pipefail

SERVICE="${SERVICE:-${1:-api}}"
cd /app/dashboard

run_api()       { exec python3 -u api_server.py; }
run_dashboard() { exec python3 -u server.py; }

case "$SERVICE" in
  api)        run_api ;;
  dashboard)  run_dashboard ;;
  both)
    python3 -u server.py     & DASH_PID=$!
    python3 -u api_server.py & API_PID=$!
    trap 'kill -TERM $DASH_PID $API_PID 2>/dev/null || true' TERM INT
    # exit as soon as either child exits, so the pod restarts cleanly
    wait -n $DASH_PID $API_PID
    EXIT=$?
    kill -TERM $DASH_PID $API_PID 2>/dev/null || true
    wait $DASH_PID $API_PID 2>/dev/null || true
    exit "$EXIT"
    ;;
  *)
    echo "unknown SERVICE='$SERVICE' (expected: api | dashboard | both)" >&2
    exit 2
    ;;
esac
