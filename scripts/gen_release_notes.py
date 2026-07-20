#!/usr/bin/env python3
"""Generate GitHub release notes from ``src/keboola_agent_cli/changelog.py``.

The release pipeline (``release-kbagent.yml``) renders the Markdown body for a
GitHub Release from the bundled ``CHANGELOG`` dict, so the release page and
``kbagent changelog`` can never disagree: the entries are emitted verbatim.

When releases are skipped (versions merged to ``main`` but never tagged), the
next tag is a catch-up release: pass the already-released versions via
``--released`` (repeatable) and every changelog version between the tag and the
first already-released one is included, one section per version, so no shipped
change ever goes unannounced. Walking the changelog beats "latest release minus
one": it needs no ordering assumptions about ``gh release list`` and an
old-line hotfix tagged after a newer release still gets exactly its own section.

The script doubles as the forward changelog gate: a *stable* version without a
``CHANGELOG`` entry is a hard error, failing the release before anything
publishes. Pre-release tags pass ``--allow-missing`` (their entry may still
ride on an unmerged feature branch, mirroring ``generate_changelog.py --check``).

Usage:
    uv run python scripts/gen_release_notes.py --version 0.71.0 \
        [--released 0.66.1 --released 0.66.0 ...] [--allow-missing] \
        [--output release-notes.md]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Collection, Mapping
from pathlib import Path

CHANGELOG_URL = "https://github.com/keboola/cli/blob/main/src/keboola_agent_cli/changelog.py"


def collect_versions(
    version: str, released: Collection[str], changelog: Mapping[str, list[str]]
) -> list[str]:
    """Return the changelog versions to include, newest first.

    Relies on ``CHANGELOG`` being ordered newest-first (documented invariant of
    the dict). Starting at ``version``, walks toward older entries and stops at
    the first one that is already in ``released`` — everything before the stop
    is an untagged version the new release must announce. ``version`` itself is
    always included even when it is in ``released`` (a pre-created release, or
    a re-run of the pipeline). When no older entry is released (empty or
    changelog-disjoint ``released``), the boundary is unknowable, so the
    conservative answer is just ``version`` — never the entire history.
    """
    keys = list(changelog)
    if version not in keys:
        return []
    start = keys.index(version)
    collected = [keys[start]]
    for key in keys[start + 1 :]:
        if key in released:
            return collected
        collected.append(key)
    return [keys[start]]


def render_notes(
    versions: list[str],
    changelog: Mapping[str, list[str]],
    prefix_re: re.Pattern[str],
) -> str:
    """Render Markdown release notes: one section per version, entries verbatim.

    ``prefix_re`` is the CLI renderer's prefix matcher
    (``commands/changelog.py:_PREFIX_RE``) so the release page bolds exactly the
    prefixes ``kbagent changelog`` colours.
    """
    lines: list[str] = []
    if len(versions) > 1:
        lines.append(
            f"Catch-up release: includes previously untagged versions "
            f"v{versions[-1]} through v{versions[0]}.\n"
        )
    for version in versions:
        lines.append(f"### v{version}\n")
        for entry in changelog[version]:
            match = prefix_re.match(entry)
            if match:
                entry = f"**{match.group(0).rstrip()}** {entry[match.end() :]}"
            lines.append(f"- {entry}")
        lines.append("")
    lines.append(
        f"Generated from the bundled changelog — the same content "
        f"`kbagent changelog --full` shows. Full history: [changelog.py]({CHANGELOG_URL})."
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Version being released (PEP 440)")
    parser.add_argument(
        "--released",
        action="append",
        default=[],
        metavar="VERSION",
        help="An already-released version (repeatable); the notes cover every "
        "changelog version between --version and the first released one",
    )
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="Exit 0 (writing nothing) when the version has no changelog entry (pre-releases)",
    )
    parser.add_argument("--output", type=Path, help="Write to this file instead of stdout")
    args = parser.parse_args()

    from keboola_agent_cli.changelog import CHANGELOG
    from keboola_agent_cli.commands.changelog import _PREFIX_RE

    versions = collect_versions(args.version, set(args.released), CHANGELOG)
    if not versions:
        if args.allow_missing:
            print(
                f"no changelog entry for {args.version}; skipping notes (pre-release)",
                file=sys.stderr,
            )
            return 0
        print(
            f"error: no CHANGELOG entry for {args.version} in "
            f"src/keboola_agent_cli/changelog.py — add one before tagging "
            f"(see the authoring contract at the top of that file)",
            file=sys.stderr,
        )
        return 1

    notes = render_notes(versions, CHANGELOG, _PREFIX_RE)
    if args.output:
        args.output.write_text(notes, encoding="utf-8")
        print(f"wrote {args.output} ({', '.join(versions)})", file=sys.stderr)
    else:
        print(notes, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
