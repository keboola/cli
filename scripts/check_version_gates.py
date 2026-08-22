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

Usage:
    python scripts/check_version_gates.py           # verify, exit 1 on drift
    python scripts/check_version_gates.py --list    # print the gate inventory
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

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


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from keboola_agent_cli.changelog import CHANGELOG

    gates = collect_gates(resolve_paths())

    if "--list" in sys.argv:
        for version in sorted(gates, key=lambda v: [int(p) for p in v.split(".")]):
            mark = " " if version in CHANGELOG else "  <-- UNKNOWN"
            print(f"{version:>10}  {len(gates[version]):>3} marker(s){mark}")
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

    total = sum(len(locs) for locs in gates.values())
    print(f"All {total} version gates across {len(gates)} versions resolve to a release.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
