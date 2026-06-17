#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PID_FILE="data/qdii-monitor.pid"
if [[ ! -f "$PID_FILE" ]]; then
  echo "QDII monitor is not running (PID file not found)."
  exit 0
fi

STORED_PID="$(tr -d '[:space:]' < "$PID_FILE")"
if [[ ! "$STORED_PID" =~ ^[0-9]+$ ]]; then
  echo "Invalid PID file: $PID_FILE" >&2
  exit 1
fi

if ! kill -0 "$STORED_PID" 2>/dev/null; then
  rm -f "$PID_FILE"
  echo "QDII monitor process is no longer running; stale PID file removed."
  exit 0
fi

CMDLINE="$(ps -p "$STORED_PID" -o args= 2>/dev/null || true)"
if [[ "$CMDLINE" != *"uvicorn qdii_monitor.app:app"* ]]; then
  echo "PID $STORED_PID does not belong to the QDII monitor; refusing to stop it." >&2
  exit 1
fi

kill "$STORED_PID"
rm -f "$PID_FILE"
echo "QDII monitor stopped (PID $STORED_PID)."

