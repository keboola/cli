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
from dataclasses import dataclass
from pathlib import Path

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent

# The silent-drift surfaces from CONTRIBUTING.md's "Plugin synchronization map".
# Anything an AI agent reads to decide whether a command exists belongs here.
#
# ``src/**/*.py`` is in scope because a version gate written in a Python
# comment is agent-facing documentation exactly like a markdown one -- and its
# absence is not hypothetical: ``(since vNEXT)`` in ``permissions.py`` survived
# the 0.90.1 release precisely because nothing under ``src/`` was scanned
# (``commands/context.py`` used to be listed alone; this glob subsumes it).
# Its first run also surfaced a stale ``(since v0.26.1)`` in ``commands/
# project.py`` -- a version that never shipped, left behind by a renumber.
#
# ``scripts/*.py`` is deliberately NOT scanned. This module has to NAME the
# placeholder it is looking for (its ``--release`` usage line and the
# ``VNEXT_TOKEN`` constant), so it would flag itself forever -- a self-
# referencing failure no regex can tell apart from a real gate. Do not retry it.
SCANNED_GLOBS: tuple[str, ...] = (
    "CLAUDE.md",
    "docs/*.md",
    "plugins/kbagent/**/*.md",
    "plugins/kbagent/.claude-plugin/CLAUDE.md",
    "src/**/*.py",
)

# ``(since v0.84.0)`` / ``(since 0.84.0)`` and ``0.73.0+`` / ``v0.73.0+``.
# The negative lookbehind keeps ``0.1.2.3+`` and ``e0.73.0+`` from matching.
GATE_RE = re.compile(r"\(since v?(\d+\.\d+\.\d+)\)|(?<![\w.])v?(\d+\.\d+\.\d+)\+")

# The placeholder a feature PR writes when it cannot know its release version.
VNEXT_TOKEN = "vNEXT"

# An inline-code span, single- OR double-backtick. Stripping these before
# looking for VNEXT_TOKEN is what separates a live gate from prose quoting the
# token (see the module docstring for why this must not be applied to GATE_RE).
# The double-backtick alternative must come FIRST -- regex alternation is
# left-biased, so a single-backtick-first pattern would match the empty span
# between the two opening backticks of ``x`` and leave the token exposed.
# It matters because Python docstrings under ``src/`` use the RST convention:
# a ``(since vNEXT)`` written there is prose, not a gate, exactly as in markdown.
INLINE_CODE_RE = re.compile(r"``[^`]*``|`[^`]*`")

# Fenced blocks are deliberately NOT stripped, even though the same "code means
# quotation" argument seems to apply. Measured: CLAUDE.md's `## All CLI
# Commands` section is one giant fenced block carrying real agent-facing gates,
# so skipping fences drops 2 of the 16 live gates in the pre-0.90.0 tree --
# silently. The trade is asymmetric: a false positive (a doc wanting to SHOW
# `(since vNEXT)` inside a fence) is a loud CI failure someone fixes in a
# minute, while a false negative is precisely the shipped-broken-gate bug this
# check exists to prevent. Use inline backticks for such an example instead.


@dataclass(frozen=True)
class VnextResidue:
    """One unresolved ``vNEXT`` gate: where it is and what the line says."""

    path: str
    line: int
    text: str


def find_vnext_residue(paths: list[Path]) -> list[VnextResidue]:
    """Return every unresolved ``vNEXT`` gate in the given files.

    A ``vNEXT`` that survives only inside an inline-code span is prose about
    the placeholder and is skipped. Pure apart from reading the given files.
    """
    residue: list[VnextResidue] = []
    for path in paths:
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        # UTF-8 is pinned, never left to the platform default: on Windows that
        # default is cp1252, and 73 of the files this now scans carry non-ASCII
        # (em dashes, box-drawing rules in section comments). Decoded as cp1252
        # their bytes turn to mojibake, which can move or destroy the backticks
        # INLINE_CODE_RE keys on -- so a real placeholder could read as quoted
        # prose on one OS and as residue on another.
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if VNEXT_TOKEN not in line:
                continue
            if VNEXT_TOKEN in INLINE_CODE_RE.sub("", line):
                residue.append(VnextResidue(path=rel, line=lineno, text=line.strip()))
    return residue


# An ATX markdown heading: 1-6 hashes followed by a space (CommonMark requires
# the space, so ``#tag`` is not a heading). Only ``.md`` files are considered --
# ``src/**/*.py`` is scanned for gates too, but a ``#`` there opens a comment,
# which has no anchor slug to break.
HEADING_RE = re.compile(r"^ {0,3}#{1,6} ")


def find_heading_placeholders(paths: list[Path]) -> list[VnextResidue]:
    """Return every markdown heading carrying a live ``vNEXT`` placeholder.

    Resolving the placeholder rewrites the heading, which rewrites its
    generated anchor slug, which breaks every inbound ``#...`` link. Unlike
    :func:`find_vnext_residue`, this is fatal on EVERY PR rather than only in
    release mode: the rule used to be a hand-run grep at release time, and in
    0.91.0 that grep lost a merge race (PR #697 ran it two minutes before #694
    and #696 landed their own headings). Checking at authoring time is what
    makes the race impossible.

    Numeric versions in headings are deliberately NOT flagged: an already
    resolved tag never changes again, so its slug is stable, and flagging the
    dozen historical ones would be noise with no inbound link at risk.
    """
    flagged: list[VnextResidue] = []
    for path in paths:
        if path.suffix != ".md":
            continue
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            if VNEXT_TOKEN not in line or not HEADING_RE.match(line):
                continue
            # Same quotation rule as the residue scan: a heading that merely
            # names the token in backticks is prose about the placeholder.
            if VNEXT_TOKEN in INLINE_CODE_RE.sub("", line):
                flagged.append(VnextResidue(path=rel, line=lineno, text=line.strip()))
    return flagged


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
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1
        ):
            for match in GATE_RE.finditer(line):
                version = match.group(1) or match.group(2)
                gates[version].append((rel, lineno))
    return dict(gates)


# Matches the bare placeholder token. ``vNEXT+`` needs no special case: the
# token is replaced in place, so ``vNEXT+`` becomes ``0.91.0+`` on its own.
VNEXT_SUB_RE = re.compile(re.escape(VNEXT_TOKEN))


def _replace_outside_code(line: str, version: str) -> tuple[str, int]:
    """Substitute the placeholder only in the parts of *line* outside code spans.

    Rewriting whole lines is what makes a blanket ``sed`` unsafe: a line may
    carry a quoted mention AND a live gate at once (CLAUDE.md's description of
    the placeholder is exactly that), and only the live one may change.
    """
    pieces: list[str] = []
    replaced = 0
    pos = 0
    for span in INLINE_CODE_RE.finditer(line):
        chunk, count = VNEXT_SUB_RE.subn(version, line[pos : span.start()])
        pieces.append(chunk)
        replaced += count
        pieces.append(span.group(0))  # the code span itself is preserved verbatim
        pos = span.end()
    chunk, count = VNEXT_SUB_RE.subn(version, line[pos:])
    pieces.append(chunk)
    replaced += count
    return "".join(pieces), replaced


def resolve_vnext(paths: list[Path], version: str) -> list[VnextResidue]:
    """Rewrite every live ``vNEXT`` gate in *paths* to *version*, in place.

    Returns one entry per rewritten LINE, carrying the text as it now reads.
    Files with nothing to change are not written at all, so a release PR's
    diff shows only the files that actually carry a gate.

    This is the mechanical form of release checklist step 4. The scanner
    already tells a live gate from prose with perfect precision; having a
    human apply that knowledge by hand across ~54 lines only adds error.
    """
    try:
        Version(version)
    except Exception as exc:  # packaging raises InvalidVersion
        raise ValueError(f"{version!r} is not a valid PEP 440 version") from exc

    changed: list[VnextResidue] = []
    for path in paths:
        try:
            rel = path.relative_to(REPO_ROOT).as_posix()
        except ValueError:
            rel = path.as_posix()
        original = path.read_text(encoding="utf-8")
        if VNEXT_TOKEN not in original:
            continue
        out_lines: list[str] = []
        file_changed = False
        for lineno, line in enumerate(original.splitlines(keepends=True), start=1):
            if VNEXT_TOKEN not in line:
                out_lines.append(line)
                continue
            new_line, count = _replace_outside_code(line, version)
            out_lines.append(new_line)
            if count:
                file_changed = True
                changed.append(VnextResidue(path=rel, line=lineno, text=new_line.strip()))
        if file_changed:
            path.write_text("".join(out_lines), encoding="utf-8")
    return changed


def gates_below(
    gates: dict[str, list[tuple[str, int]]], floor: str
) -> dict[str, list[tuple[str, int]]]:
    """Return the gates naming a version older than *floor*, oldest version first.

    A gate only earns its place while some live install predates it. kbagent
    self-updates on startup, so for a sufficiently old version that population
    rounds to zero -- while the gate keeps making the agent refuse a command
    the user actually has. Raising a floor and de-tagging below it is periodic
    maintenance; this produces the worklist.

    The floor itself is NOT below the floor: it is the oldest version still
    worth gating for.
    """
    limit = Version(floor)
    below = {v: locs for v, locs in gates.items() if Version(v) < limit}
    return {v: below[v] for v in sorted(below, key=Version)}


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
    heading_residue = find_heading_placeholders(paths)

    if "--resolve" in sys.argv:
        index = sys.argv.index("--resolve") + 1
        if index >= len(sys.argv):
            print("ERROR: --resolve needs a version argument (e.g. --resolve 0.91.0)")
            return 1
        requested = sys.argv[index].strip()
        shipped = _pyproject_version()
        # Cross-check against pyproject rather than trusting the format alone:
        # `packaging` accepts `v0.91` and `0.91`, so a typo can parse cleanly
        # and then be stamped into every gate in the tree at once.
        if requested.lstrip("v") != shipped:
            print(
                f"ERROR: --resolve {requested} disagrees with pyproject.toml ({shipped}).\n\n"
                "Resolve gates to the version this tree actually ships. Bump\n"
                "pyproject.toml first, then re-run.\n"
            )
            return 1
        try:
            applied = resolve_vnext(paths, shipped)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 1
        if not applied:
            print(f"No unresolved '{VNEXT_TOKEN}' gates to rewrite.")
            return 0
        print(f"Rewrote {len(applied)} '{VNEXT_TOKEN}' gate(s) to {shipped}:\n")
        for gate in applied:
            print(f"    {gate.path}:{gate.line}")
        still = find_heading_placeholders(resolve_paths())
        if still:  # pragma: no cover - defensive; headings are fatal earlier
            print(f"\nWARNING: {len(still)} placeholder(s) remain in headings.")
        return 0

    if "--list-below" in sys.argv:
        index = sys.argv.index("--list-below") + 1
        if index >= len(sys.argv):
            print("ERROR: --list-below needs a version argument (e.g. --list-below 0.80.0)")
            return 1
        floor = sys.argv[index].strip().lstrip("v")
        stale = gates_below(gates, floor)
        total = sum(len(locs) for locs in stale.values())
        print(f"{total} gate(s) across {len(stale)} version(s) below the {floor} floor:\n")
        for version in stale:
            print(f"  {version}")
            for rel, lineno in stale[version]:
                print(f"    {rel}:{lineno}")
        return 0

    if "--list" in sys.argv:
        for version in sorted(gates, key=lambda v: [int(p) for p in v.split(".")]):
            mark = " " if version in CHANGELOG else "  <-- UNKNOWN"
            print(f"{version:>10}  {len(gates[version]):>3} marker(s){mark}")
        print(f"{VNEXT_TOKEN:>10}  {len(residue):>3} marker(s)  <-- unresolved placeholder")
        print(f"{'in headings':>10}  {len(heading_residue):>3} marker(s)  <-- always fatal")
        return 0

    # Fatal in EVERY mode, unlike the plain residue below: a placeholder in a
    # heading is never correct at any point in the release cycle, and deferring
    # the complaint to the release PR is exactly how 0.91.0 shipped three of
    # them (the hand-run grep in #697 raced #694 and #696).
    if heading_residue:
        print(
            f"ERROR: {len(heading_residue)} '{VNEXT_TOKEN}' placeholder(s) inside a "
            "markdown heading.\n"
        )
        print(
            "Resolving the placeholder rewrites the heading, which rewrites its\n"
            "generated anchor slug and breaks every inbound '#...' link to the\n"
            "section. Move the tag onto the section's first body line instead:\n"
            "\n"
            "    ## Ignored components\n"
            "\n"
            f"    *(since {VNEXT_TOKEN}, #689)*\n"
        )
        for gate in heading_residue:
            print(f"    {gate.path}:{gate.line}")
            print(f"        {gate.text[:100]}")
        return 1

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
        for gate in residue:
            print(f"    {gate.path}:{gate.line}")
            print(f"        {gate.text[:100]}")
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
