"""One-shot migration: replace error_code="LITERAL" with error_code=ErrorCode.LITERAL.

Run once from the repo root:
    python scripts/migrate_error_codes.py

Safe to re-run (idempotent: already-migrated sites are unchanged).
"""

import re
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src"
ERRORS_MODULE = "keboola_agent_cli.errors"

# All valid enum members (keep in sync with errors.py).
VALID_CODES = {
    "INVALID_TOKEN",
    "ACCESS_DENIED",
    "PERMISSION_DENIED",
    "TIMEOUT",
    "CONNECTION_ERROR",
    "RETRY_EXHAUSTED",
    "API_ERROR",
    "NOT_FOUND",
    "ALREADY_EXISTS",
    "VALIDATION_ERROR",
    "INVALID_ARGUMENT",
    "INVALID_FORMAT",
    "USAGE_ERROR",
    "MISSING_PARAMETER",
    "UNKNOWN_ERROR",
    "CONFIG_ERROR",
    "NOT_INITIALIZED",
    "INIT_ERROR",
    "QUEUE_JOB_FAILED",
    "QUEUE_JOB_TIMEOUT",
    "STORAGE_JOB_FAILED",
    "STORAGE_JOB_TIMEOUT",
    "QUERY_JOB_FAILED",
    "QUERY_JOB_TIMEOUT",
    "NO_VARIABLE_ROWS",
    "MALFORMED_VARIABLES_ROW",
    "UPLOAD_FAILED",
    "EXPORT_EMPTY_MANIFEST",
    "EXPORT_NO_FILE",
    "EXPORT_NO_URL",
    "NOT_SLICED",
    "FILE_NOT_FOUND",
    "DIR_NOT_FOUND",
    "READ_ERROR",
    "WRITE_ERROR",
    "INPUT_ERROR",
    "NODE_NOT_FOUND",
    "INVALID_SHARING_TYPE",
    "NOT_LINKED_BUCKET",
    "KAI_ERROR",
    "KAI_NOT_ENABLED",
    "MISSING_QUERY",
    "WORKSPACE_NOT_FOUND",
    "PARENT_CONFIG_NOT_TRACKED",
    "FILE_NO_URL",
    "ENCRYPTION_FAILED",
}

LITERAL_PATTERN = re.compile(r'error_code\s*=\s*"([A-Z_]+)"')

IMPORT_LINE = "from .errors import ErrorCode"
IMPORT_LINE_DOTDOT = "from ..errors import ErrorCode"


def _relative_import_for(path: Path) -> str:
    """Return the correct relative import for the given source file."""
    rel = path.relative_to(SRC_ROOT / "keboola_agent_cli")
    depth = len(rel.parts) - 1  # 0 for top-level, 1 for commands/, services/, etc.
    if depth == 0:
        return "from .errors import ErrorCode"
    return "from ..errors import ErrorCode"


def _ensure_import(source: str, import_line: str) -> str:
    """Insert the ErrorCode import if not already present."""
    if "ErrorCode" in source:
        return source
    # Insert after the last existing 'from ... import' or 'import ...' line near the top.
    lines = source.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")) and "errors" in line:
            # Insert right after the existing errors import
            insert_at = i + 1
            break
        if line.startswith(("import ", "from ")):
            insert_at = i + 1
    lines.insert(insert_at, import_line + "\n")
    return "".join(lines)


def migrate_file(path: Path) -> bool:
    """Replace string literals in *path*. Returns True if the file was changed."""
    original = path.read_text(encoding="utf-8")
    source = original

    codes_used: set[str] = set()

    def replace(m: re.Match) -> str:
        code = m.group(1)
        if code not in VALID_CODES:
            print(f"  WARN: unknown code '{code}' in {path} -- skipping", file=sys.stderr)
            return m.group(0)
        codes_used.add(code)
        # Preserve spacing around '=' from the original match.
        eq_part = m.group(0).split("error_code")[1].split('"')[0]  # e.g. ' = ' or '='
        return f"error_code{eq_part}ErrorCode.{code}"

    source = LITERAL_PATTERN.sub(replace, source)

    if codes_used:
        import_line = _relative_import_for(path)
        source = _ensure_import(source, import_line)

    if source != original:
        path.write_text(source, encoding="utf-8")
        return True
    return False


def main() -> None:
    changed = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        if py_file.name == "errors.py":
            continue  # skip the definition file itself
        if migrate_file(py_file):
            changed.append(py_file)

    if changed:
        print(f"Migrated {len(changed)} files:")
        for f in changed:
            print(f"  {f.relative_to(SRC_ROOT.parent.parent)}")
    else:
        print("Nothing to migrate.")


if __name__ == "__main__":
    main()
