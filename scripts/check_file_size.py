"""CI guard: enforce the per-layer file-size budgets from CONTRIBUTING.md.

Budgets are measured in **code lines**, not raw line count: docstrings,
comments and blank lines are excluded. Raw LOC would tax the long
rationale-carrying docstrings this codebase deliberately writes -- they are
what makes it navigable, so a metric that punishes them pushes in exactly the
wrong direction. Measured on the 0.78.0 tree, `version_service.py` is 1259
lines but only 657 lines of code; 36% of the file is prose.

What counts as a code line: any physical line carrying at least one token that
is not a comment, a docstring, or pure layout (NEWLINE/NL/INDENT/DEDENT). A
module-level string *assigned to a name* (the CHANGELOG tables, SQL blocks) is
data, not prose, and is counted -- only true docstrings, i.e. the bare leading
string of a module, class, or function, are exempt.

Usage (run from repo root):
    python scripts/check_file_size.py            # exits 1 if any HARD ceiling is exceeded
    python scripts/check_file_size.py --report   # print every file, largest first
    python scripts/check_file_size.py --top 20   # report mode, limited

Exit codes: 0 = all within hard ceilings (soft-ceiling overruns are warnings
only, printed but non-fatal); 1 = at least one hard ceiling exceeded.
"""

import argparse
import ast
import io
import json
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PKG_ROOT = REPO_ROOT / "src" / "keboola_agent_cli"

# Tokens that never make a line "code" on their own.
_LAYOUT_TOKENS = frozenset(
    {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
)


@dataclass(frozen=True)
class Budget:
    """Per-layer code-line ceilings.

    ``soft`` is advisory -- crossing it means the next PR adding material to the
    file should split it first. ``hard`` blocks: splitting is required before
    more functionality lands.
    """

    label: str
    soft: int
    hard: int


# Ordered: the FIRST matching prefix wins, so specific layers precede the
# catch-all. Numbers carried over from the CONTRIBUTING.md table -- switching
# the metric from raw LOC to code lines already relaxes them for well-commented
# files, which is the intent; they were not additionally loosened.
_BUDGETS: tuple[tuple[str, Budget], ...] = (
    ("commands/", Budget("commands", soft=800, hard=1200)),
    ("services/", Budget("services", soft=1000, hard=1500)),
    ("client/", Budget("client", soft=1500, hard=2000)),
    ("manage_client.py", Budget("client", soft=1500, hard=2000)),
    ("server/", Budget("server", soft=800, hard=1200)),
    ("sync/", Budget("sync", soft=1000, hard=1500)),
)
# Everything else under the package: top-level modules, helpers, generated data.
_DEFAULT_BUDGET = Budget("module", soft=1000, hard=1500)

# Files exempt from the ceiling, with the reason. Keep this list SHORT and
# justified -- an exemption is an admission the budget does not model the file.
# Both entries below are documentation payloads that happen to live in .py
# files: a ceiling on them would only push prose out of the repo.
_EXEMPT: dict[str, str] = {
    "changelog.py": (
        "append-only release-note data, one block per version; splitting it "
        "would just move the append point without reducing anything"
    ),
    "commands/context.py": (
        "a single AGENT_CONTEXT string literal (~1600 lines of CLI documentation "
        "served by `kbagent context`); it grows with every new command by design"
    ),
}

# Ratchet baseline: files that already exceeded their hard ceiling when the
# check was introduced (0.78.0). They are grandfathered at their recorded size
# and may only shrink -- growth fails the check. This is what lets the gate be
# blocking on day one without a repo-wide refactor first: it stops NEW debt and
# stops existing debt getting worse, rather than demanding it all be paid now.
BASELINE_PATH = REPO_ROOT / "scripts" / "file_size_baseline.json"


@dataclass(frozen=True)
class FileMetrics:
    """Line accounting for one Python file."""

    path: Path
    total: int
    code: int
    docstring: int
    comment: int
    blank: int

    @property
    def prose_ratio(self) -> float:
        """Share of the file that is docstring or comment."""
        return (self.docstring + self.comment) / self.total if self.total else 0.0


def _docstring_lines(tree: ast.Module) -> set[int]:
    """Physical line numbers occupied by true docstrings.

    Only the bare leading string of a module / class / function qualifies --
    :func:`ast.get_docstring` semantics. A string assigned to a name is data
    and stays counted as code.
    """
    lines: set[int] = set()
    scopes: tuple[type[ast.AST], ...] = (
        ast.Module,
        ast.ClassDef,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
    )
    for node in ast.walk(tree):
        if not isinstance(node, scopes):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if not isinstance(first, ast.Expr) or not isinstance(first.value, ast.Constant):
            continue
        if not isinstance(first.value.value, str):
            continue
        end = first.end_lineno or first.lineno
        lines.update(range(first.lineno, end + 1))
    return lines


def measure(path: Path) -> FileMetrics:
    """Count code / docstring / comment / blank lines in one file."""
    source = path.read_text(encoding="utf-8")
    total = len(source.splitlines())
    doc_lines = _docstring_lines(ast.parse(source))

    code_lines: set[int] = set()
    comment_lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            comment_lines.add(token.start[0])
            continue
        if token.type in _LAYOUT_TOKENS:
            continue
        code_lines.update(range(token.start[0], token.end[0] + 1))

    code_lines -= doc_lines
    comment_lines -= code_lines  # a trailing comment rides on a code line
    blank = sum(1 for line in source.splitlines() if not line.strip())
    return FileMetrics(
        path=path,
        total=total,
        code=len(code_lines),
        docstring=len(doc_lines),
        comment=len(comment_lines),
        blank=blank,
    )


def budget_for(relative_path: str) -> Budget:
    """Resolve the budget for a package-relative path."""
    for prefix, budget in _BUDGETS:
        if relative_path.startswith(prefix) or relative_path == prefix:
            return budget
    return _DEFAULT_BUDGET


def _iter_package_files() -> list[Path]:
    """Every checked-in Python module in the package, excluding caches."""
    return sorted(p for p in PKG_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _report(metrics: list[FileMetrics], limit: int | None) -> None:
    """Print every file largest-first with its budget headroom."""
    ranked = sorted(metrics, key=lambda m: m.code, reverse=True)
    if limit is not None:
        ranked = ranked[:limit]
    print(f"{'file':52} {'code':>6} {'total':>6} {'prose':>6}  budget")
    for metric in ranked:
        rel = metric.path.relative_to(PKG_ROOT).as_posix()
        budget = budget_for(rel)
        if rel in _EXEMPT or metric.path.name in _EXEMPT:
            state = "exempt"
        elif metric.code > budget.hard:
            state = f"HARD >{budget.hard}"
        elif metric.code > budget.soft:
            state = f"soft >{budget.soft}"
        else:
            state = f"ok ({budget.soft}/{budget.hard})"
        print(
            f"{rel:52} {metric.code:6} {metric.total:6} "
            f"{metric.prose_ratio:5.0%}  {budget.label}: {state}"
        )


def _is_exempt(relative_path: str) -> bool:
    return relative_path in _EXEMPT


def _load_baseline() -> dict[str, int]:
    """Read the grandfathered sizes, or an empty ratchet if none is recorded."""
    if not BASELINE_PATH.is_file():
        return {}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {str(k): int(v) for k, v in data.get("files", {}).items()}


def _write_baseline(metrics: list[FileMetrics]) -> dict[str, int]:
    """Record every currently-over-ceiling file at its present size."""
    recorded: dict[str, int] = {}
    for metric in sorted(metrics, key=lambda m: m.path.as_posix()):
        rel = metric.path.relative_to(PKG_ROOT).as_posix()
        if _is_exempt(rel):
            continue
        if metric.code > budget_for(rel).hard:
            recorded[rel] = metric.code
    payload = {
        "_comment": (
            "Grandfathered files over their CONTRIBUTING.md hard ceiling, in CODE LINES "
            "(see scripts/check_file_size.py). They may only shrink. Regenerate with "
            "`make loc-baseline` after a split -- never to silence a file you just grew."
        ),
        "files": recorded,
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return recorded


def main(argv: list[str] | None = None) -> int:
    """Run the gate. ``argv`` defaults to the process arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true", help="list every file, largest first")
    parser.add_argument("--top", type=int, default=None, help="limit --report to N files")
    parser.add_argument(
        "--update-baseline",
        action="store_true",
        help="re-record the grandfathered sizes (run after a split)",
    )
    args = parser.parse_args(argv)

    metrics = [measure(path) for path in _iter_package_files()]

    if args.report or args.top is not None:
        _report(metrics, args.top)
        return 0

    if args.update_baseline:
        recorded = _write_baseline(metrics)
        print(f"Recorded {len(recorded)} grandfathered files in {BASELINE_PATH.name}.")
        return 0

    baseline = _load_baseline()
    new_debt: list[tuple[FileMetrics, Budget]] = []
    regressions: list[tuple[FileMetrics, int]] = []
    healed: list[str] = []
    over_soft: list[tuple[FileMetrics, Budget]] = []

    for metric in metrics:
        rel = metric.path.relative_to(PKG_ROOT).as_posix()
        if _is_exempt(rel):
            continue
        budget = budget_for(rel)
        allowance = baseline.get(rel)
        if allowance is not None:
            # Grandfathered: the ceiling is its recorded size, and it may only shrink.
            if metric.code > allowance:
                regressions.append((metric, allowance))
            elif metric.code <= budget.hard:
                healed.append(rel)
            continue
        if metric.code > budget.hard:
            new_debt.append((metric, budget))
        elif metric.code > budget.soft:
            over_soft.append((metric, budget))

    for metric, budget in sorted(over_soft, key=lambda pair: pair[0].code, reverse=True):
        rel = metric.path.relative_to(PKG_ROOT).as_posix()
        print(
            f"WARN: {rel} is {metric.code} code lines, over the {budget.label} soft "
            f"ceiling of {budget.soft}. The next PR adding material here should split it first."
        )
    for rel in sorted(healed):
        print(f"NOTE: {rel} is back within its ceiling -- drop it via `make loc-baseline`.")

    if new_debt or regressions:
        print()
        for metric, budget in sorted(new_debt, key=lambda pair: pair[0].code, reverse=True):
            rel = metric.path.relative_to(PKG_ROOT).as_posix()
            print(
                f"FAIL: {rel} is {metric.code} code lines, over the {budget.label} HARD "
                f"ceiling of {budget.hard}. Split it before merging more functionality."
            )
        for metric, allowance in sorted(regressions, key=lambda pair: pair[0].code, reverse=True):
            rel = metric.path.relative_to(PKG_ROOT).as_posix()
            print(
                f"FAIL: {rel} grew to {metric.code} code lines, past its grandfathered "
                f"{allowance}. This file is already over budget -- shrink it, do not extend it."
            )
        print("\nSee CONTRIBUTING.md > 'File-size budgets'. `--report` shows the whole tree.")
        return 1

    checked = len(metrics) - len(_EXEMPT)
    print(
        f"OK: {checked} modules within budget "
        f"({len(over_soft)} over soft, {len(baseline)} grandfathered)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
