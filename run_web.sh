#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

HOST_ADDRESS="${WEB_HOST:-127.0.0.1}"
PORT="${WEB_PORT:-8010}"
PID_FILE="data/qdii-monitor.pid"
LOG_FILE="${QDII_LOG_FILE:-data/qdii-monitor.log}"

if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PYTHON="$(command -v python)"
else
  echo "Python not found. Create .venv and install requirements.txt first." >&2
  exit 1
fi

mkdir -p "$(dirname "$PID_FILE")"
mkdir -p "$(dirname "$LOG_FILE")"

if [[ -f "$PID_FILE" ]]; then
  STORED_PID="$(tr -d '[:space:]' < "$PID_FILE")"
  if [[ "$STORED_PID" =~ ^[0-9]+$ ]] && kill -0 "$STORED_PID" 2>/dev/null; then
    CMDLINE="$(ps -p "$STORED_PID" -o args= 2>/dev/null || true)"
    if [[ "$CMDLINE" == *"uvicorn qdii_monitor.app:app"* ]]; then
      echo "QDII monitor already running (PID $STORED_PID)." >&2
      exit 1
    fi
  fi
  rm -f "$PID_FILE"
fi

echo "Starting QDII monitor at http://$HOST_ADDRESS:$PORT"
nohup "$PYTHON" -m uvicorn qdii_monitor.app:app --host "$HOST_ADDRESS" --port "$PORT" > "$LOG_FILE" 2>&1 &
CHILD_PID=$!
echo "$CHILD_PID" > "$PID_FILE"
echo "PID: $CHILD_PID"
echo "Log: $LOG_FILE"
