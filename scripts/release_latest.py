#!/usr/bin/env python3
"""Decide the GitHub "latest" flag of kbagent releases across a major version change.

The release plan for a new major (CONTRIBUTING.md, "Releasing a new major version"):

- T0 of major N is the ``published_at`` of the stable GitHub release ``vN.0.0``.
- M of major N is T0 + 28 days. Until M, ``releases/latest`` keeps naming the newest
  release of major N-1, so the clients that follow ``releases/latest`` stay on N-1.
- From M, the newest release of major N is the latest release.

The release workflow and the ``promote-major`` workflow read their decision from this
script, so both use one rule. Both subcommands only read the GitHub REST API and print
``key=value`` lines for ``$GITHUB_OUTPUT``:

``classify --version X.Y.Z [--prerelease]``
    ``make_latest``: the ``make_latest`` value for the release of this version.
    ``old_line``: ``true`` when the next major is already published, i.e. the tag is
    a fix from a ``release/<major>.<minor>`` branch after T0. The Homebrew and ai-kit
    jobs skip such a tag, because they would move those channels back to the old major.

``promote``
    ``tag``: the release to mark as latest now, or empty when nothing must change.

"Now" is the ``Date`` header of the GitHub response, the same clock the clients use
for M. A 404 means "not published"; any other API error stops the script with a
non-zero exit code, so the workflow fails instead of guessing.

Usage:
    GH_TOKEN=... GITHUB_REPOSITORY=keboola/cli python3 scripts/release_latest.py classify --version 1.0.0
    GH_TOKEN=... GITHUB_REPOSITORY=keboola/cli python3 scripts/release_latest.py promote
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

GITHUB_API = "https://api.github.com"
MIGRATION_WINDOW = timedelta(days=28)
RELEASE_LIST_PAGE_SIZE = 100
HTTP_TIMEOUT_SECONDS = 30
# One attempt plus three retries on a 5xx, a 429 or a network error, then fail.
RETRY_DELAYS_SECONDS = (2, 4, 8)

# A stable kbagent tag: vX.Y.Z or vX.Y.Z.postN (the release workflow treats .post as
# a real release). Pre-release markers (a/b/rc/.dev/-suffix) do not match.
STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:\.post(\d+))?$")
MAJOR_PREFIX = re.compile(r"^v?(\d+)\.")

Version = tuple[int, int, int, int]


@dataclass(frozen=True)
class Release:
    tag: str
    published_at: datetime


def parse_stable(tag: str) -> Version | None:
    """``v1.2.3`` -> ``(1, 2, 3, 0)``; ``None`` for anything that is not a stable tag."""
    match = STABLE_TAG.match(tag)
    if match is None:
        return None
    major, minor, patch, post = match.groups()
    return int(major), int(minor), int(patch), int(post or 0)


def major_of(version: str) -> int:
    """The major number of a version string, pre-releases included (``0.43.0b1`` -> 0)."""
    match = MAJOR_PREFIX.match(version)
    if match is None:
        raise ValueError(f"not a kbagent version: {version!r}")
    return int(match.group(1))


def decide_make_latest(
    version: str,
    *,
    prerelease: bool,
    own_major_start: datetime | None,
    next_major_start: datetime | None,
    current_latest: Version | None,
    now: datetime,
) -> bool:
    """The ``make_latest`` value for the release of ``version``.

    ``own_major_start`` is T0 of the version's own major (ignored for major 0, which
    has no start); ``next_major_start`` is T0 of the next major. ``None`` means that
    release is not published.
    """
    if prerelease:
        return False
    parsed = parse_stable(f"v{version}")
    if parsed is None:
        raise ValueError(f"not a stable kbagent version: {version!r}")
    # A new major is not latest before its own M (and v<N>.0.0 itself is not
    # published yet while its release is being created).
    if parsed[0] > 0 and (own_major_start is None or now < own_major_start + MIGRATION_WINDOW):
        return False
    # An old major is not latest from the M of the next major.
    if next_major_start is not None and now >= next_major_start + MIGRATION_WINDOW:
        return False
    # Never move the flag back to a lower version (a re-run of an older tag).
    return current_latest is None or current_latest <= parsed


def promotion_target(
    releases: list[Release],
    starts: dict[int, datetime | None],
    current_latest_tag: str | None,
    now: datetime,
) -> str | None:
    """The tag to mark as latest now, or ``None`` when the flag is already right.

    ``starts`` maps a major to T0 (``published_at`` of ``v<major>.0.0``, ``None`` if
    not published). The target is the newest stable release in ``releases`` of the
    highest major whose M has passed. Before any M (including before T0) there is
    no target.
    """
    promoted = [
        major
        for major, start in starts.items()
        if major >= 1 and start is not None and now >= start + MIGRATION_WINDOW
    ]
    if not promoted:
        return None
    major = max(promoted)
    candidates = [
        (parsed, release.tag)
        for release in releases
        if (parsed := parse_stable(release.tag)) is not None and parsed[0] == major
    ]
    if not candidates:
        return None
    target = max(candidates)[1]
    return None if target == current_latest_tag else target


class GitHubReader:
    """Read-only GitHub REST client that remembers the server time of the last answer."""

    def __init__(self, repo: str, token: str) -> None:
        self.repo = repo
        self.token = token
        self.now: datetime | None = None

    def get(self, path: str) -> Any | None:
        """JSON body of ``GET /repos/<repo>/<path>``; ``None`` on 404."""
        request = urllib.request.Request(
            f"{GITHUB_API}/repos/{self.repo}/{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        for delay in (*RETRY_DELAYS_SECONDS, None):
            try:
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    self._remember_date(response.headers)
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    self._remember_date(exc.headers)
                    return None
                if delay is None or not (exc.code == 429 or exc.code >= 500):
                    raise
            except (urllib.error.URLError, TimeoutError):
                if delay is None:
                    raise
            print(f"GET {path} failed; retrying in {delay} s", file=sys.stderr)
            time.sleep(delay)
        raise AssertionError("unreachable")

    def _remember_date(self, headers: Message) -> None:
        self.now = parsedate_to_datetime(headers["Date"])

    def server_now(self) -> datetime:
        if self.now is None:
            raise RuntimeError("no GitHub response read yet")
        return self.now

    def major_start(self, major: int) -> datetime | None:
        """``published_at`` of the stable release ``v<major>.0.0``; ``None`` if not published."""
        data = self.get(f"releases/tags/v{major}.0.0")
        if data is None or data["draft"] or data["prerelease"] or not data["published_at"]:
            return None
        return datetime.fromisoformat(data["published_at"])

    def latest_tag(self) -> str | None:
        data = self.get("releases/latest")
        return None if data is None else data["tag_name"]

    def stable_releases(self) -> list[Release]:
        """The newest stable releases (one page). Only the newest release of each
        major is needed from it; T0 is read separately (``major_start``)."""
        data = self.get(f"releases?per_page={RELEASE_LIST_PAGE_SIZE}") or []
        return [
            Release(tag=item["tag_name"], published_at=datetime.fromisoformat(item["published_at"]))
            for item in data
            if not item["draft"] and not item["prerelease"] and item["published_at"]
        ]


def classify(reader: GitHubReader, version: str, *, prerelease: bool) -> dict[str, str]:
    major = major_of(version)
    own_major_start = reader.major_start(major) if major > 0 else None
    next_major_start = reader.major_start(major + 1)
    latest_tag = reader.latest_tag()
    make_latest = decide_make_latest(
        version,
        prerelease=prerelease,
        own_major_start=own_major_start,
        next_major_start=next_major_start,
        current_latest=parse_stable(latest_tag) if latest_tag else None,
        now=reader.server_now(),
    )
    return {
        "make_latest": str(make_latest).lower(),
        "old_line": str(next_major_start is not None).lower(),
    }


def promote(reader: GitHubReader) -> dict[str, str]:
    """T0 comes from ``releases/tags/v<N>.0.0``, not from the release list, so it does
    not matter how old ``v<N>.0.0`` is. The majors are read from the highest one
    down, and the loop stops at the first major whose M has passed: usually one or
    two calls.
    """
    releases = reader.stable_releases()
    latest_tag = reader.latest_tag()
    now = reader.server_now()
    majors = sorted(
        {parsed[0] for r in releases if (parsed := parse_stable(r.tag)) and parsed[0] >= 1},
        reverse=True,
    )
    starts: dict[int, datetime | None] = {}
    for major in majors:
        start = reader.major_start(major)
        starts[major] = start
        if start is not None and now >= start + MIGRATION_WINDOW:
            break
    return {"tag": promotion_target(releases, starts, latest_tag, now) or ""}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Decide the GitHub latest flag of kbagent releases."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    classify_parser = sub.add_parser("classify", help="make_latest and old_line of one release")
    classify_parser.add_argument("--version", required=True, help="version without the v prefix")
    classify_parser.add_argument("--prerelease", action="store_true")
    sub.add_parser("promote", help="the tag to mark as latest now (empty: nothing to do)")
    args = parser.parse_args(argv)

    reader = GitHubReader(os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])
    if args.command == "classify":
        result = classify(reader, args.version, prerelease=args.prerelease)
    else:
        result = promote(reader)
    for key, value in result.items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
