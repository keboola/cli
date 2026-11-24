#!/usr/bin/env bash
# Start the frozen binary with `serve --ui` and require GET / to answer 200.
# Catches a freeze without the `server` extras (#1018), where serve exits with
# "missing: uvicorn".
# Usage: check_serve_ui.sh <path to the binary, without .exe>
set -euo pipefail
BIN="$1"
[ -f "${BIN}.exe" ] && BIN="${BIN}.exe"
PORT=8765
CONFIG_DIR="$(mktemp -d)"
LOG="$(mktemp)"

"$BIN" serve --ui --port "$PORT" --no-banner --config-dir "$CONFIG_DIR" >"$LOG" 2>&1 &
PID=$!

STATUS=""
for _ in $(seq 1 60); do
  kill -0 "$PID" 2>/dev/null || break
  STATUS="$(curl -s --max-time 2 -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/" || true)"
  [ "$STATUS" = "200" ] && break
  sleep 1
done

kill "$PID" 2>/dev/null || true

if [ "$STATUS" != "200" ]; then
  echo "serve --ui did not answer GET / with 200 (last status: ${STATUS:-none}). Server output:"
  cat "$LOG"
  exit 1
fi
echo "serve --ui answered GET / with 200."
