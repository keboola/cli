#!/usr/bin/env bash
# Spin up all three web/ dev processes in one terminal.
#
# - Generates a fresh KBAGENT_SERVE_TOKEN (or reuses one from env).
# - Starts kbagent serve on :8001 (Python).
# - Starts the Node BFF on :8000 (forwards /api/* to kbagent serve with auth).
# - Starts the Vite dev server on :5173 (React HMR).
#
# Press Ctrl+C and all three children get killed (trap below).
#
# Usage:  ./scripts/web-dev.sh [--config-dir /tmp/kbagent/.kbagent]
#         make web-dev

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Capture the caller's invocation cwd BEFORE any `cd`. ``kbagent serve``
# is launched with this cwd so any nested-sync workspace it creates
# (`kbagent sync init/pull <alias>` -> `<cwd>/<alias>/`) lands in the
# operator's working directory, not the keboola_agent_cli repo root.
# All other child processes (Vite, BFF) continue to need REPO_DIR.
USER_CWD="${PWD}"

CONFIG_DIR_FLAG=""
if [[ "${1:-}" == "--config-dir" && -n "${2:-}" ]]; then
  CONFIG_DIR_FLAG="--config-dir $2"
fi

# Reuse KBAGENT_SERVE_TOKEN if the user already exported one (handy for
# attaching extra clients), otherwise mint a fresh one.
TOKEN="${KBAGENT_SERVE_TOKEN:-$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')}"
export KBAGENT_SERVE_TOKEN="${TOKEN}"

cleanup() {
  echo ""
  echo "[web-dev] shutting down children..."
  jobs -p | xargs -r kill 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "${REPO_DIR}"

echo "[web-dev] kbagent serve token: ${TOKEN}"
echo "[web-dev] starting kbagent serve on :8001 (Python) -- cwd: ${USER_CWD}"
# Run uv from REPO_DIR (so it finds the project's pyproject.toml + venv)
# but execute kbagent in USER_CWD so nested-sync workspaces land where
# the operator launched the server. uv's --project flag resolves the
# project root explicitly so we can chdir freely.
(cd "${USER_CWD}" && uv run --project "${REPO_DIR}" kbagent serve --port 8001 --log-level warning ${CONFIG_DIR_FLAG}) 2>&1 \
  | sed -u 's/^/[serve] /' &

# Wait for kbagent serve to come up before launching the BFF (which would
# otherwise fail its first proxy check).
for _ in $(seq 1 30); do
  if curl -fs http://127.0.0.1:8001/health/ping >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

echo "[web-dev] starting Node BFF on :8000"
(cd "${REPO_DIR}/web/backend" && PORT=8000 KBAGENT_SERVE_TOKEN="${TOKEN}" npm run dev) 2>&1 \
  | sed -u 's/^/[bff]   /' &

echo "[web-dev] starting Vite dev server on :5173"
(cd "${REPO_DIR}/web/frontend" && npm run dev) 2>&1 \
  | sed -u 's/^/[vite]  /' &

echo ""
echo "[web-dev] -- ready --"
echo "[web-dev] Open http://localhost:5173"
echo "[web-dev] Press Ctrl+C to stop everything"
echo ""

wait
