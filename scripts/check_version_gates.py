#!/usr/bin/env python3
"""Reject a version gate that names a version no release will ever carry.

Agent-facing docs annotate flags with the release that introduced them --
``(since v0.84.0)`` in ``gotchas.md``, ``0.73.0+`` in the command reference.
``plugins/kbagent/agents/keboola-expert.md`` turns those into a hard rule: the
agent compares the user's installed ``kbagent version`` against the gate and
REFUSES anything newer. A gate naming a version that never ships therefore
makes the agent refuse a flag the user actually has, and nothing else in the
repo notices -- ``CONTRIBUTING.md``'s "Plugin synchronization map" lists these
files as silent-drift risks precisely because no test covered them.

The concrete way this happens is a release renumber. Two PRs each bump
``pyproject.toml`` on their way to main, neither is published, and collapsing
the span into one release leaves every ``0.89.0+`` marker pointing at a version
that no longer exists. That is a mechanical, greppable mistake, so grep for it.

Scope is deliberately narrow: only the two explicit gate SYNTAXES are checked,
never a bare version-looking string. Prose carries plenty of numbers that are
not gates -- an upstream ``keboola-mcp-server v1.76.2``, ``manifest v3``,
``RFC 8628`` -- and flagging those would make the check unusable. The cost of
that choice is a stale bare mention (``# 0.89.0 MCP-parity flags`` in a test
comment) passing silently; those mislead a reader, not the agent's version gate.

A second, related failure mode lives here too: the ``vNEXT`` PLACEHOLDER.
A feature PR does not know its release version, so it tags new behavior
``(since vNEXT)`` and the release PR rewrites every one to the version
shipping. Miss one and the plugin -- served to agents straight from the repo
-- carries a gate no ``kbagent version`` can ever satisfy, which is strictly
worse than no gate at all: the agent refuses a command the user has. That
rewrite was checklist-only until now (``CONTRIBUTING.md`` step 4), and its
grep could never come back empty, because the process docs necessarily
*mention* the placeholder they are describing.

Backticks settle that. A ``vNEXT`` inside an inline-code span is quoting the
literal token -- prose ABOUT the mechanism; outside one it is a live gate.
Fenced blocks are NOT exempt; see ``find_vnext_residue`` for the measurement
that settled it.
Verified against the pre-0.90.0 tree: 16 real gates detected, 4 prose
mentions ignored, no manual allowlist needed.

That rule is deliberately NOT applied to the numeric gates above. There,
backticks are ordinary typography: ``docs/sdk.md`` writes 14 genuine gates as
```0.66.0+``, and stripping code spans would hide every one. The asymmetry is
real, not an oversight -- ``vNEXT`` is a placeholder token that prose quotes,
a version number is a value that prose formats.

Residue is reported as INFO on every run and only FAILS under ``--release``,
because a feature PR is *supposed* to carry ``vNEXT``. CI applies that flag
only to a PR that changes ``pyproject.toml``'s version, i.e. a release PR.

Usage:
    python scripts/check_version_gates.py           # verify, exit 1 on drift
    python scripts/check_version_gates.py --list    # print the gate inventory
    python scripts/check_version_gates.py --release # ALSO fail on vNEXT residue
    python scripts/check_version_gates.py --release-if-newer-than 0.89.0
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent

# The silent-drift surfaces from CONTRIBUTING.md's "Plugin synchronization map".
# Anything an AI agent reads to decide whether a command exists belongs here.
SCANNED_GLOBS: tuple[str, ...] = (
    "CLAUDE.md",
    "docs/*.md",
    "plugins/kbagent/**/*.md",
    "plugins/kbagent/.claude-plugin/CLAUDE.md",
    "src/keboola_agent_cli/commands/context.py",
)

# ``(since v0.84.0)`` / ``(since 0.84.0)`` and ``0.73.0+`` / ``v0.73.0+``.
# The negative lookbehind keeps ``0.1.2.3+`` and ``e0.73.0+`` from matching.
GATE_RE = re.compile(r"\(since v?(\d+\.\d+\.\d+)\)|(?<![\w.])v?(\d+\.\d+\.\d+)\+")

# The placeholder a feature PR writes when it cannot know its release version.
VNEXT_TOKEN = "vNEXT"

# An inline-code span. Stripping these before looking for VNEXT_TOKEN is what
# separates a live gate from prose quoting the token (see the module docstring
# for why this must not be applied to GATE_RE).
INLINE_CODE_RE = re.compile(r"`[^`]*`")

# Fenced blocks are deliberately NOT stripped, even though the same "code means
# quotation" argument seems to apply. Measured: CLAUDE.md's `## All CLI
# Commands` section is one giant fenced block carrying real agent-facing gates,
# so skipping fences drops 2 of the 16 live gates in the pre-0.90.0 tree --
# silently. The trade is asymmetric: a false positive (a doc wanting to SHOW
# `(since vNEXT)` inside a fence) is a loud CI failure someone fixes in a
# minute, while a false negative is precisely the shipped-broken-gate bug this
# check exists to prevent. Use inline backticks for such an example instead.


def find_vnext_residue(paths: list[Path]) -> list[tuple[str, int, str]]:
    """Return every unresolved ``vNEXT`` gate as ``(relative path, line, text)``.

    A ``vNEXT`` that survives only inside an inline-code span is prose about
    the placeholder and is skipped. Pure apart from reading the given files.
    """
    residue: list[tuple[str, int, str]] = []
    for path in paths:
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            if VNEXT_TOKEN not in line:
                continue
            if VNEXT_TOKEN in INLINE_CODE_RE.sub("", line):
                residue.append((rel, lineno, line.strip()))
    return residue


def collect_gates(paths: list[Path]) -> dict[str, list[tuple[str, int]]]:
    """Map each gated version to the ``(relative path, line number)`` naming it.

    Pure apart from reading the given files, so the caller controls the file
    set and tests can point it at a fixture tree.
    """
    gates: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for path in paths:
        # A caller may point this at a fixture tree outside the repo; report an
        # absolute path there rather than raising on ``relative_to``.
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), start=1):
            for match in GATE_RE.finditer(line):
                version = match.group(1) or match.group(2)
                gates[version].append((rel, lineno))
    return dict(gates)


def resolve_paths() -> list[Path]:
    """Expand SCANNED_GLOBS into an ordered, de-duplicated file list."""
    seen: dict[Path, None] = {}
    for pattern in SCANNED_GLOBS:
        for path in sorted(REPO_ROOT.glob(pattern)):
            if path.is_file():
                seen.setdefault(path, None)
    return list(seen)


def _pyproject_version() -> str:
    """Read the version this working tree declares."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("ERROR: could not read version from pyproject.toml")
    return match.group(1)


def is_release_mode(argv: list[str]) -> bool:
    """Decide whether unresolved ``vNEXT`` should be fatal for this invocation.

    ``--release`` forces it. ``--release-if-newer-than BASE`` lets CI hand over
    the base branch's version and self-select: only a branch that RAISES the
    version is a release PR.

    The comparison matters. A two-dot diff against the base tip also fires for
    a stale feature branch whose base has since been released -- there the
    version moved DOWN, and failing it would tell a contributor to delete a
    placeholder they are required to write. PEP 440 ordering (``packaging``,
    already a runtime dependency) also keeps ``0.10.0`` above ``0.9.0`` and
    reads ``0.90.0b1`` as a release of ``0.89.0``'s successor.
    """
    if "--release" in argv:
        return True
    if "--release-if-newer-than" not in argv:
        return False
    index = argv.index("--release-if-newer-than") + 1
    if index >= len(argv):
        raise SystemExit("ERROR: --release-if-newer-than needs a version argument")
    base = argv[index].strip().lstrip("v")
    if not base:
        # An unreadable base (new file, first commit) must not silently disarm
        # the gate NOR fail an ordinary PR -- treat it as "not a release".
        return False
    return Version(_pyproject_version()) > Version(base)


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from keboola_agent_cli.changelog import CHANGELOG

    paths = resolve_paths()
    gates = collect_gates(paths)
    residue = find_vnext_residue(paths)

    if "--list" in sys.argv:
        for version in sorted(gates, key=lambda v: [int(p) for p in v.split(".")]):
            mark = " " if version in CHANGELOG else "  <-- UNKNOWN"
            print(f"{version:>10}  {len(gates[version]):>3} marker(s){mark}")
        print(f"{VNEXT_TOKEN:>10}  {len(residue):>3} marker(s)  <-- unresolved placeholder")
        return 0

    unknown = {v: locs for v, locs in gates.items() if v not in CHANGELOG}
    if unknown:
        print("ERROR: version gate names a version with no changelog entry.\n")
        print(
            "An agent reading these refuses commands the user's installed "
            "kbagent actually has.\n"
            "If a release was renumbered, rewrite the markers to the version "
            "that shipped.\n"
        )
        for version in sorted(unknown):
            print(f"  {version} is not a CHANGELOG key -- named by:")
            for rel, lineno in unknown[version]:
                print(f"    {rel}:{lineno}")
        return 1

    if residue and is_release_mode(sys.argv):
        print(f"ERROR: {len(residue)} unresolved '{VNEXT_TOKEN}' version gate(s) in a release.\n")
        print(
            "A release PR must rewrite every placeholder to the version it ships.\n"
            "Left in, the plugin ships agents a gate no installed version can satisfy,\n"
            "so they refuse commands the user actually has. Rewrite these, then re-run:\n"
        )
        for rel, lineno, text in residue:
            print(f"    {rel}:{lineno}")
            print(f"        {text[:100]}")
        return 1

    total = sum(len(locs) for locs in gates.values())
    print(f"All {total} version gates across {len(gates)} versions resolve to a release.")
    if residue:
        # INFO, not a failure: a feature PR is SUPPOSED to carry the placeholder.
        # `--release` (CI applies it to a version-bumping PR) is what makes it fatal.
        print(
            f"{len(residue)} unresolved '{VNEXT_TOKEN}' gate(s) awaiting the next release PR "
            "(run with --release to list and enforce)."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
