#!/usr/bin/env bash
# Post-edit quality gate: ruff fix/format + ty type-check on a single file.
# Invoked by Claude Code's PostToolUse hook (.claude/settings.json) after
# Edit/Write/MultiEdit on Python files.
#
# Reads tool input JSON from stdin (Claude Code hook contract). Extracts
# the edited file path; if it is a .py file under our project, runs:
#   1. ruff check --fix --quiet  (auto-fix safe lint issues)
#   2. ruff format --quiet       (canonical formatting)
#   3. ty check                  (type errors -- reported as warnings)
#
# Exit code 0 = continue. Non-zero = Claude sees the failure as a tool result
# and must address it before next action (per CONTRIBUTING.md "Code Quality
# Patterns").
#
# Manual invocation for debugging:
#   echo '{"tool_input":{"file_path":"src/keboola_agent_cli/cli.py"}}' \
#     | scripts/post-edit-quality.sh

set -euo pipefail

PAYLOAD="$(cat)"

FILE_PATH="$(printf '%s' "$PAYLOAD" \
    | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get("tool_input",{}).get("file_path",""))' \
    2>/dev/null || true)"

if [ -z "$FILE_PATH" ]; then
    exit 0
fi

case "$FILE_PATH" in
    *.py) ;;
    *) exit 0 ;;
esac

if [ ! -f "$FILE_PATH" ]; then
    exit 0
fi

# Run from repo root so ruff/ty pick up pyproject.toml config.
REPO_ROOT="$(git -C "$(dirname "$FILE_PATH")" rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
    exit 0
fi
cd "$REPO_ROOT"

FAILED=0

if ! uv run --quiet ruff check --fix --quiet "$FILE_PATH"; then
    echo "post-edit: ruff check found unresolved issues in $FILE_PATH" >&2
    FAILED=1
fi

uv run --quiet ruff format --quiet "$FILE_PATH" || true

# ty: report but do NOT fail the hook -- type checker is in warning-only mode
# while we migrate. Switch this to FAILED=1 when the codebase is clean.
if ! uv run --quiet ty check "$FILE_PATH" 2>&1 | tail -5; then
    : # warnings only
fi

exit $FAILED
