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


def _check_mode() -> None:
    """Verify all GitHub releases have entries in changelog.py."""
    from keboola_agent_cli.changelog import CHANGELOG

    result = subprocess.run(
        ["gh", "release", "list", "--limit", "50", "--json", "tagName"],
        capture_output=True,
        text=True,
        check=True,
    )
    tags = json.loads(result.stdout)

    missing = []
    for entry in tags:
        version = entry["tagName"].lstrip("v")
        if version not in CHANGELOG:
            missing.append(version)

    if missing:
        print(f"Missing changelog entries for: {', '.join(missing)}")
        sys.exit(1)
    else:
        print(f"All {len(tags)} releases have changelog entries.")


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
