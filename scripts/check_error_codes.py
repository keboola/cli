"""CI guard: reject raw error_code string literals in source files.

Any site that passes error_code="LITERAL_STRING" to KeboolaApiError,
ConfigError, or formatter.error() must use ErrorCode.<MEMBER> instead.

Usage (run from repo root):
    python scripts/check_error_codes.py          # exits 1 if violations found
    python scripts/check_error_codes.py --list   # print all current enum members

Safe exceptions (not flagged):
  - tests/           -- string comparisons in assertions are fine
  - errors.py        -- the enum definition itself
"""

import ast
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).parent.parent / "src"
SKIP_FILES = {"errors.py"}


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
        # Print all known enum members without importing the package
        errors_path = SRC_ROOT / "keboola_agent_cli" / "errors.py"
        source = errors_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "ErrorCode":
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for t in item.targets:
                            if isinstance(t, ast.Name):
                                print(f"  ErrorCode.{t.id}")
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

    print("OK: no raw error_code string literals in source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
