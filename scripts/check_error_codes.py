"""CI guard: reject raw error_code string literals in source files.

Any site that passes error_code="LITERAL_STRING" to KeboolaApiError,
ConfigError, or formatter.error() must use ErrorCode.<MEMBER> instead.

Also verifies docs/error-codes.md completeness: every ErrorCode enum member
must appear in the doc's code catalogue (as a `` `CODE` `` table cell) and
vice versa. The doc is publicly linked from help.keboola.com, so drift ships
broken documentation.

Spec rule `errors.code-has-type-and-exit` (docs/cli-interface-spec.md#E1):
every ErrorCode member has an entry in ERROR_CODE_TABLE (errors.py) with a
known error_type and an exit code from the spec exit-code table, and the
`Exit` and `Type` columns of docs/error-codes.md equal the table.
`scripts/check_cli_spec.py` reports the same rule through
`code_table_violations()`.

Usage (run from repo root):
    python scripts/check_error_codes.py          # exits 1 if violations found
    python scripts/check_error_codes.py --list   # print all current enum members

Safe exceptions (not flagged):
  - tests/           -- string comparisons in assertions are fine
  - errors.py        -- the enum definition itself
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src"
ERRORS_PATH = SRC_ROOT / "keboola_agent_cli" / "errors.py"
DOC_PATH = REPO_ROOT / "docs" / "error-codes.md"
SKIP_FILES = {"errors.py"}
# The exit codes of docs/cli-interface-spec.md#exit-codes that an error can
# have: 0 is success, so it is never an error code's exit. 130 is an interrupt
# and belongs to INTERRUPTED only.
ERROR_EXIT_CODES = frozenset({1, 2, 3, 4, 5, 6, 7})
INTERRUPT_EXIT_CODE = 130
_DOC_ROW = re.compile(r"^\| `([A-Z][A-Z0-9_]*)` \| (\d+) \| `([a-z_]+)` \|", re.MULTILINE)


def _enum_members() -> set[str]:
    """Parse ErrorCode members from errors.py without importing the package."""
    tree = ast.parse(ERRORS_PATH.read_text(encoding="utf-8"))
    members: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ErrorCode":
            for item in node.body:
                if isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            members.add(t.id)
    return members


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _documented_codes() -> set[str]:
    """Parse code names from docs/error-codes.md table rows (`` | `CODE` | ... ``)."""
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", _doc_text(), re.MULTILINE))


def _check_doc_completeness() -> bool:
    """Return True when the enum and docs/error-codes.md list the same codes."""
    enum_codes = _enum_members()
    doc_codes = _documented_codes()
    ok = True
    missing = sorted(enum_codes - doc_codes)
    if missing:
        ok = False
        print(f"  docs/error-codes.md is missing {len(missing)} enum member(s):")
        for code in missing:
            print(f"    {code}")
    stale = sorted(doc_codes - enum_codes)
    if stale:
        ok = False
        print(f"  docs/error-codes.md documents {len(stale)} unknown code(s):")
        for code in stale:
            print(f"    {code}")
    return ok


def code_table_violations() -> dict[str, str]:
    """Subject -> reason for every breach of `errors.code-has-type-and-exit`."""
    from keboola_agent_cli.errors import ERROR_CODE_TABLE, ERROR_TYPES

    found: dict[str, str] = {}
    for code in sorted(_enum_members() - set(ERROR_CODE_TABLE)):
        found[code] = "no entry in ERROR_CODE_TABLE (errors.py)"
    for code in sorted(set(ERROR_CODE_TABLE) - _enum_members()):
        found[code] = "ERROR_CODE_TABLE has an entry for a code that is not an ErrorCode member"
    documented = {m[0]: (int(m[1]), m[2]) for m in _DOC_ROW.findall(_doc_text())}
    for code, entry in sorted(ERROR_CODE_TABLE.items()):
        if entry.error_type not in ERROR_TYPES:
            found[code] = f"error_type {entry.error_type!r} is not in ERROR_TYPES"
        elif entry.exit_code not in ERROR_EXIT_CODES and (
            code != "INTERRUPTED" or entry.exit_code != INTERRUPT_EXIT_CODE
        ):
            found[code] = f"exit code {entry.exit_code} is not an error exit code of the spec"
        elif code in _enum_members() and documented.get(code) != (
            entry.exit_code,
            entry.error_type,
        ):
            found[code] = (
                f"docs/error-codes.md says {documented.get(code)}, ERROR_CODE_TABLE says "
                f"({entry.exit_code}, {entry.error_type!r}). The doc row is "
                "| `CODE` | <exit> | `<type>` | <description> |"
            )
    return found


def _collect_violations(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, code) for each raw error_code string literal."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "error_code":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                violations.append((kw.value.lineno, kw.value.value))
    return violations


def main() -> int:
    if "--list" in sys.argv:
        for member in sorted(_enum_members()):
            print(f"  ErrorCode.{member}")
        return 0

    found_any = False
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        if py_file.name in SKIP_FILES:
            continue
        violations = _collect_violations(py_file)
        if violations:
            found_any = True
            rel = py_file.relative_to(SRC_ROOT.parent.parent)
            for lineno, code in violations:
                print(f'  {rel}:{lineno}: error_code="{code}" -- use ErrorCode.{code}')

    if found_any:
        print("\nFAIL: raw error_code string literals found. Replace with ErrorCode.<MEMBER>.")
        return 1

    if not _check_doc_completeness():
        print("\nFAIL: docs/error-codes.md is out of sync with the ErrorCode enum.")
        return 1

    table_violations = code_table_violations()
    if table_violations:
        for code, reason in table_violations.items():
            print(f"  {code}: {reason}")
        print(
            "\nFAIL: errors.code-has-type-and-exit -- every ErrorCode needs an explicit "
            "(error_type, exit_code) entry that docs/error-codes.md repeats "
            "(docs/cli-interface-spec.md#E1)."
        )
        return 1

    print(
        "OK: no raw error_code literals; docs/error-codes.md matches the enum and "
        "the error code table."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
