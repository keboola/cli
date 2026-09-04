#!/usr/bin/env python3
"""Generate changelog skeleton from GitHub releases.

Fetches release notes from the GitHub API and prints a Python dict
suitable for pasting into ``src/keboola_agent_cli/changelog.py``.

Usage:
    python scripts/generate_changelog.py          # print skeleton
    python scripts/generate_changelog.py --check  # verify all releases have entries
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path


def _fetch_releases() -> list[dict]:
    """Fetch all releases from GitHub via ``gh`` CLI."""
    result = subprocess.run(
        ["gh", "release", "list", "--limit", "50", "--json", "tagName"],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(result.stdout)

    releases = []
    for tag_entry in tags:
        tag = tag_entry["tagName"]
        view_result = subprocess.run(
            ["gh", "release", "view", tag, "--json", "body,tagName"],
            capture_output=True,
            text=True,
            check=True,
        )
        releases.append(json.loads(view_result.stdout))
    return releases


def _extract_summary(body: str) -> list[str]:
    """Extract H2/H3 headings as summary lines from release body."""
    lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped.startswith(("## ", "### ")):
            heading = stripped.lstrip("#").strip()
            # Skip generic headings
            if heading.lower() in (
                "what's new",
                "what's changed",
                "what changed",
                "install / upgrade",
                "upgrade",
                "documentation",
                "docs",
                "tests",
                "contributors",
                "acknowledgements",
                "thanks",
                "related",
            ):
                continue
            lines.append(heading)
    return lines


def audit_changelog_coverage(
    tags: list[dict], changelog: Mapping[str, object]
) -> tuple[list[str], int, int]:
    """Split release tags into (missing, checked, skipped) against the changelog.

    Pure helper (no I/O) so it is unit-testable. ``tags`` are
    ``gh release list --json tagName,isPrerelease`` entries.

    Pre-releases (PEP 440 betas / rcs, e.g. ``v0.44.0b1``) are skipped: per the
    CONTRIBUTING.md beta workflow they are tagged on a feature branch and their
    ``CHANGELOG`` entry rides along on that branch until the PR merges, so
    ``main`` must not demand it. This mirrors the auto-update path, which only
    sees stable releases via GitHub's ``/releases/latest`` and ignores
    prereleases.

    Returns ``(missing_versions, stable_checked_count, prerelease_skipped_count)``.
    """
    missing = []
    checked = 0
    skipped = 0
    for entry in tags:
        if entry.get("isPrerelease"):
            skipped += 1
            continue
        checked += 1
        version = entry["tagName"].lstrip("v")
        if version not in changelog:
            missing.append(version)
    return missing, checked, skipped


# Changelog versions that were bumped into ``main`` but never published as a
# GitHub release -- 18 of them, against 161 published releases. Their content
# did reach users, folded into the next release's wheel, but that release's
# notes never mentioned it, so those features shipped silently.
#
# Frozen rather than fixed: publishing a tag retroactively would advertise a
# wheel nobody can install, and rewriting old release notes does not reach
# anyone who already read them. The point of the check below is to stop this
# list from growing -- do not add to it. If a version bump has landed with no
# release, either publish it or fold its entry into the version being prepared.
KNOWN_UNRELEASED: frozenset[str] = frozenset(
    {
        "0.47.0",
        "0.47.2",
        "0.48.0",
        "0.51.1",
        "0.60.1",
        "0.60.2",
        "0.60.3",
        "0.60.4",
        "0.63.3",
        "0.63.4",
        "0.67.0",
        "0.68.0",
        "0.69.0",
        "0.70.0",
        "0.70.1",
        "0.78.0",
        "0.81.0",
        "0.83.0",
    }
)


def _version_key(version: str) -> tuple[int, ...]:
    """Sort/compare versions numerically (``0.9.0`` < ``0.10.0``)."""
    return tuple(int(part) for part in re.findall(r"\d+", version))


def audit_release_coverage(
    tags: list[dict], changelog: Mapping[str, object], current_version: str
) -> list[str]:
    """Return changelog versions that were never published as a release.

    The inverse of :func:`audit_changelog_coverage`, and the direction that
    actually bites. A version bump lands in ``main`` with every PR that touches
    ``pyproject.toml``, but publishing is a separate manual step -- so a
    merge-train can leave two changelog blocks behind and the next tag silently
    absorbs both. Whoever writes that tag's release notes reads one block and
    ships the other unannounced.

    ``current_version`` (from ``pyproject.toml``) is the release being prepared
    and is expected to have no tag yet, so it is never reported.

    Pure (no I/O) so it is unit-testable.
    """
    published = {entry["tagName"].lstrip("v") for entry in tags}
    if not published:
        return []
    # ``gh release list`` is windowed by ``--limit``. Auditing a changelog key
    # older than the oldest tag that window contains would report every early
    # release as unpublished, so the audit floor is that tag rather than the
    # start of the changelog.
    floor = min(_version_key(v) for v in published)
    return sorted(
        (
            version
            for version in changelog
            if version not in published
            and version != current_version
            and version not in KNOWN_UNRELEASED
            and _version_key(version) >= floor
        ),
        key=_version_key,
    )


def _read_current_version() -> str:
    """Read ``version`` out of ``pyproject.toml`` without a TOML dependency."""
    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    for line in pyproject.read_text().splitlines():
        match = re.match(r'^version\s*=\s*"([^"]+)"', line)
        if match:
            return match.group(1)
    raise RuntimeError("no version found in pyproject.toml")


def _check_mode() -> None:
    """Verify releases and changelog entries cover each other, both ways."""
    from keboola_agent_cli.changelog import CHANGELOG

    result = subprocess.run(
        ["gh", "release", "list", "--limit", "500", "--json", "tagName,isPrerelease"],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(result.stdout)

    missing, checked, skipped = audit_changelog_coverage(tags, CHANGELOG)
    unreleased = audit_release_coverage(tags, CHANGELOG, _read_current_version())

    suffix = f" ({skipped} pre-release(s) skipped)" if skipped else ""
    failed = False
    if missing:
        print(f"Missing changelog entries for: {', '.join(missing)}{suffix}")
        failed = True
    if unreleased:
        print(
            f"Changelog entries with no published release: {', '.join(unreleased)}\n"
            "  Their content ships inside the NEXT release, whose notes will not\n"
            "  mention it -- the features go out silently. Either publish these\n"
            "  tags, or fold the entries into the version being prepared."
        )
        failed = True
    if failed:
        sys.exit(1)
    print(
        f"All {checked} stable releases have changelog entries, "
        f"and every changelog entry has a release.{suffix}"
    )


def main() -> None:
    if "--check" in sys.argv:
        _check_mode()
        return

    releases = _fetch_releases()

    print("CHANGELOG: dict[str, list[str]] = {")
    for release in releases:
        tag = release["tagName"]
        version = tag.lstrip("v")
        body = release.get("body", "")
        summaries = _extract_summary(body)

        if summaries:
            entries = ", ".join(f'"{s}"' for s in summaries)
            print(f'    "{version}": [{entries}],')
        else:
            print(f'    "{version}": ["TODO: add summary"],')
    print("}")


if __name__ == "__main__":
    main()
