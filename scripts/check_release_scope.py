#!/usr/bin/env python3
"""Prove the new changelog entry covers every PR the release tag will contain.

``make changelog-check`` answers a different question: does every *released
version* have a changelog entry? It never asks whether that entry covers every
*commit* under the tag. The gap is not hypothetical -- it shipped in v0.91.0.

PR #625 merged to ``main`` at 14:27, after the release PR had branched off and
before the release PR itself merged at 14:36. The changelog entry had been
authored against the earlier scope, so #625 -- a new plugin slash command plus
a rewritten onboarding flow -- sat inside the tag's tree with no release note
of any kind. It was caught only because the tag happened to be deferred.

The window is structural: a release PR is open for as long as its CI takes,
and that is exactly when parallel feature PRs land. So the scope collected when
the release PR is *opened* is not the scope the tag will *contain*, and the
only trustworthy moment to compare them is immediately before tagging.

Usage::

    python scripts/check_release_scope.py                    # v<last>..HEAD
    python scripts/check_release_scope.py --base v0.90.1
    python scripts/check_release_scope.py --head origin/main
    python scripts/check_release_scope.py --ignore-pr 699    # repeatable

Run it in the release PR *before* merging and no PR needs ignoring: the release
PR's own number is not in the log until its merge commit exists. Run it after
merging (against ``origin/main``) and the release PR itself shows up as a miss
-- that is what ``--ignore-pr`` is for.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[1]

# GitHub's squash-merge subject ends in ``(#N)``; the "Merge pull request #N"
# subject is the merge-commit form. A ``(#N)`` anywhere else in a subject is a
# cross-reference to another PR ("follow-up to (#686)"), not this commit's own,
# so only the trailing form counts.
_TRAILING_PR_RE = re.compile(r"\(#(\d+)\)\s*$")
_MERGE_COMMIT_RE = re.compile(r"^\S+\s+Merge pull request #(\d+)\b")

# Any GitHub number cited in a changelog bullet -- a PR decoration like
# ``Fix (#686, #694):`` or an issue named in prose. Both are evidence the
# release notes account for the work.
_ANY_REF_RE = re.compile(r"#(\d+)")


def merged_pr_numbers(log_text: str) -> list[str]:
    """Return the PR numbers in ``git log --oneline --first-parent`` output.

    Order is preserved and duplicates are dropped, so the report reads in the
    same order as the log the caller can eyeball.
    """
    found: dict[str, None] = {}
    for line in log_text.splitlines():
        match = _TRAILING_PR_RE.search(line) or _MERGE_COMMIT_RE.match(line)
        if match:
            found.setdefault(match.group(1), None)
    return list(found)


def referenced_pr_numbers(notes: list[str]) -> set[str]:
    """Return every GitHub number cited anywhere in a release's changelog bullets."""
    return {ref for note in notes for ref in _ANY_REF_RE.findall(note)}


def missing_references(log_text: str, notes: list[str], ignore: frozenset[str]) -> list[str]:
    """Return merged PR numbers that the changelog entry never mentions."""
    referenced = referenced_pr_numbers(notes)
    return [pr for pr in merged_pr_numbers(log_text) if pr not in referenced and pr not in ignore]


def should_arm(base: str, head: str) -> bool:
    """Whether this check applies: only a PR that RAISES the version is a release PR.

    Fails open on anything unreadable or malformed. A shallow CI checkout may
    not be able to read the base branch's ``pyproject.toml`` at all, and an
    ordinary feature PR must never be blocked by that -- the cost of a missed
    arming is one manual ``make release-scope-check``, the cost of a false
    arming is every PR in the repo going red.
    """
    try:
        return Version(head) > Version(base)
    except (InvalidVersion, TypeError):
        return False


class GitUnavailable(RuntimeError):
    """Git could not answer -- typically a shallow CI checkout with no tags."""


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        raise GitUnavailable(f"git {' '.join(args)} failed") from exc


def _last_release_tag() -> str:
    """The most recent tag reachable from HEAD, i.e. the release being built on."""
    return _git("describe", "--tags", "--abbrev=0", "--match", "v*").strip()


def _arg(name: str, default: str | None = None) -> str | None:
    if name not in sys.argv:
        return default
    index = sys.argv.index(name) + 1
    if index >= len(sys.argv):
        raise SystemExit(f"ERROR: {name} needs an argument")
    return sys.argv[index]


def _ignored() -> frozenset[str]:
    ignored: set[str] = set()
    for index, token in enumerate(sys.argv):
        if token == "--ignore-pr" and index + 1 < len(sys.argv):
            ignored.add(sys.argv[index + 1].lstrip("#"))
    return frozenset(ignored)


def main() -> int:
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from keboola_agent_cli.changelog import CHANGELOG

    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        print("ERROR: could not read version from pyproject.toml")
        return 1
    version = match.group(1)

    notes = CHANGELOG.get(version)
    if notes is None:
        print(f"ERROR: pyproject.toml declares {version}, which has no changelog entry.\n")
        print("Add the entry before checking its scope (release checklist step 3).")
        return 1

    arm_base = _arg("--only-if-newer-than")
    if arm_base is not None and not should_arm(arm_base.strip(), version):
        print(
            f"Not a release PR (base {arm_base.strip() or '<unreadable>'} -> {version}); "
            "release-scope check not armed."
        )
        return 0

    ignore = _ignored()
    try:
        base = _arg("--base") or _last_release_tag()
        head = _arg("--head") or "HEAD"
        log_text = _git("log", f"{base}..{head}", "--oneline", "--first-parent")
    except GitUnavailable as exc:
        # Loud, but never blocking: a shallow checkout without tags cannot
        # answer this, and that must not turn into a red build on a PR whose
        # content is fine. On a release PR the checklist runs it locally.
        print(f"WARNING: release-scope check skipped -- {exc}.")
        print("  A shallow checkout has no tags/history. Run 'make release-scope-check' locally.")
        return 0
    absent = missing_references(log_text, notes, ignore)

    if absent:
        print(
            f"ERROR: {len(absent)} PR(s) merged in {base}..{head} are not in the {version} entry.\n"
        )
        print(
            "The tag will contain this work, but the release notes are rendered\n"
            "from the changelog entry -- so it would ship with no note at all.\n"
            "Add a bullet for each, or pass --ignore-pr N for the release PR itself.\n"
        )
        for pr in absent:
            subject = next(
                (line for line in log_text.splitlines() if f"#{pr})" in line or f"#{pr} " in line),
                "",
            )
            print(f"    #{pr}  {subject.split(' ', 1)[-1] if subject else ''}")
        return 1

    covered = len(merged_pr_numbers(log_text))
    print(
        f"Release scope OK: all {covered} PR(s) merged in {base}..{head} "
        f"are referenced by the {version} changelog entry."
    )
    if ignore:
        print(f"  (ignored: {', '.join('#' + pr for pr in sorted(ignore))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
