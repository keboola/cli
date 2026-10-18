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

# ty: the backlog is now fully cleared (0 diagnostics across src/tests/scripts,
# see issue #280 PR-3, shipped in 0.45.0), so the type checker BLOCKS the edit
# on any error -- a newly introduced type error must be fixed before continuing.
# Warnings (e.g. the downgraded unresolved-import rule) do NOT block.
TY_OUTPUT="$(uv run --quiet ty check "$FILE_PATH" 2>&1 || true)"
if printf '%s\n' "$TY_OUTPUT" | grep -qE '^error'; then
    echo "post-edit: ty found type errors in $FILE_PATH (blocking -- fix before continuing):" >&2
    printf '%s\n' "$TY_OUTPUT" | grep -E '^(error|warning)' | head -10 >&2
    FAILED=1
fi

# CLI spec check: a change in commands/ can break a rule of the CLI interface
# spec or leave docs/cli-manifest.json stale (`make cli-manifest`).
case "$FILE_PATH" in
    */src/keboola_agent_cli/commands/*|src/keboola_agent_cli/commands/*)
        if ! SPEC_OUTPUT="$(uv run --quiet --extra server python scripts/check_cli_spec.py 2>&1)"; then
            echo "post-edit: CLI spec check failed (blocking -- fix before continuing):" >&2
            printf '%s\n' "$SPEC_OUTPUT" | grep -E '^(FAIL|Traceback|[A-Za-z]*Error)' | head -10 >&2 || true
            if printf '%s\n' "$SPEC_OUTPUT" | grep -q '^FAIL stale'; then
                echo "post-edit: a fixed violation is still in the baseline; run 'make cli-spec-baseline'." >&2
            fi
            if printf '%s\n' "$SPEC_OUTPUT" | grep -q 'help.manifest-fresh'; then
                echo "post-edit: docs/cli-manifest.json is stale; run 'make cli-manifest'." >&2
            fi
            FAILED=1
        fi
        ;;
esac

exit $FAILED
