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
import subprocess
import sys


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
        if stripped.startswith("## ") or stripped.startswith("### "):
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
    tags: list[dict], changelog: dict[str, object]
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


def _check_mode() -> None:
    """Verify all stable GitHub releases have entries in changelog.py."""
    from keboola_agent_cli.changelog import CHANGELOG

    result = subprocess.run(
        ["gh", "release", "list", "--limit", "50", "--json", "tagName,isPrerelease"],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(result.stdout)

    missing, checked, skipped = audit_changelog_coverage(tags, CHANGELOG)

    suffix = f" ({skipped} pre-release(s) skipped)" if skipped else ""
    if missing:
        print(f"Missing changelog entries for: {', '.join(missing)}{suffix}")
        sys.exit(1)
    else:
        print(f"All {checked} stable releases have changelog entries.{suffix}")


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
