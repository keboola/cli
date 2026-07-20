"""CI guard: reject raw error_code string literals in source files.

Any site that passes error_code="LITERAL_STRING" to KeboolaApiError,
ConfigError, or formatter.error() must use ErrorCode.<MEMBER> instead.

Also verifies docs/error-codes.md completeness: every ErrorCode enum member
must appear in the doc's code catalogue (as a `` `CODE` `` table cell) and
vice versa. The doc is publicly linked from help.keboola.com, so drift ships
broken documentation.

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


def _documented_codes() -> set[str]:
    """Parse code names from docs/error-codes.md table rows (`` | `CODE` | ... ``)."""
    doc = DOC_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"^\| `([A-Z][A-Z0-9_]*)` \|", doc, re.MULTILINE))


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

    print("OK: no raw error_code literals; docs/error-codes.md matches the enum.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
