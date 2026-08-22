"""Unit tests for the changelog-coverage audit (``scripts/generate_changelog.py``).

Regression coverage for the `make changelog-check` pre-release handling: a
published pre-release tag (e.g. ``v0.44.0b1``) whose ``CHANGELOG`` entry lives
on an unmerged feature branch must NOT be reported as missing on ``main``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar

# ``scripts/`` is not an importable package; load the module by file path.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "generate_changelog.py"
_spec = importlib.util.spec_from_file_location("generate_changelog", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
generate_changelog = importlib.util.module_from_spec(_spec)
sys.modules["generate_changelog"] = generate_changelog
_spec.loader.exec_module(generate_changelog)

audit = generate_changelog.audit_changelog_coverage


class TestAuditChangelogCoverage:
    def test_stable_release_with_entry_is_not_missing(self) -> None:
        tags = [{"tagName": "v0.43.7", "isPrerelease": False}]
        missing, checked, skipped = audit(tags, {"0.43.7": ["..."]})
        assert missing == []
        assert (checked, skipped) == (1, 0)

    def test_stable_release_without_entry_is_missing(self) -> None:
        tags = [{"tagName": "v0.43.7", "isPrerelease": False}]
        missing, checked, skipped = audit(tags, {})
        assert missing == ["0.43.7"]
        assert (checked, skipped) == (1, 0)

    def test_prerelease_without_entry_is_skipped_not_missing(self) -> None:
        # The v0.44.0b1 scenario: beta tag exists on GitHub, no entry on main.
        tags = [{"tagName": "v0.44.0b1", "isPrerelease": True}]
        missing, checked, skipped = audit(tags, {})
        assert missing == []
        assert (checked, skipped) == (0, 1)

    def test_mixed_stable_and_prerelease(self) -> None:
        tags = [
            {"tagName": "v0.43.7", "isPrerelease": False},
            {"tagName": "v0.44.0b1", "isPrerelease": True},
            {"tagName": "v0.43.6", "isPrerelease": False},
        ]
        missing, checked, skipped = audit(tags, {"0.43.7": ["x"]})
        assert missing == ["0.43.6"]  # stable, no entry
        assert (checked, skipped) == (2, 1)

    def test_v_prefix_is_stripped(self) -> None:
        tags = [{"tagName": "v1.2.3", "isPrerelease": False}]
        missing, _checked, _skipped = audit(tags, {"1.2.3": ["x"]})
        assert missing == []

    def test_missing_isprerelease_key_treated_as_stable(self) -> None:
        # Defensive: a tag dict without the flag is audited as a stable release.
        tags = [{"tagName": "v0.9.9"}]
        missing, checked, skipped = audit(tags, {})
        assert missing == ["0.9.9"]
        assert (checked, skipped) == (1, 0)


audit_releases = generate_changelog.audit_release_coverage


class TestAuditReleaseCoverage:
    """The inverse audit: a changelog entry that was never published.

    A version bump rides along with any PR touching ``pyproject.toml``, but
    publishing is a separate manual step. A merge-train can therefore leave two
    changelog blocks behind, and the next tag silently absorbs both -- whoever
    writes that tag's notes reads one block and ships the other unannounced.
    """

    TAGS: ClassVar[list[dict[str, object]]] = [
        {"tagName": "v0.87.0", "isPrerelease": False},
        {"tagName": "v0.86.0", "isPrerelease": False},
        {"tagName": "v0.85.0", "isPrerelease": False},
    ]

    def test_stranded_block_is_reported(self) -> None:
        """The exact 0.88.0/0.89.0 case: two blocks, one tag about to be cut."""
        changelog = {"0.89.0": [], "0.88.0": [], "0.87.0": [], "0.86.0": [], "0.85.0": []}
        assert audit_releases(self.TAGS, changelog, "0.88.0") == ["0.89.0"]

    def test_merged_block_is_clean(self) -> None:
        changelog = {"0.88.0": [], "0.87.0": [], "0.86.0": [], "0.85.0": []}
        assert audit_releases(self.TAGS, changelog, "0.88.0") == []

    def test_version_being_prepared_is_never_reported(self) -> None:
        """The release under preparation has no tag yet -- that is the point."""
        changelog = {"0.88.0": [], "0.87.0": []}
        assert audit_releases(self.TAGS, changelog, "0.88.0") == []

    def test_entry_older_than_the_fetched_window_is_ignored(self) -> None:
        """``gh release list`` is ``--limit``-windowed.

        Auditing a key older than the oldest tag in that window would report
        every early release as unpublished.
        """
        changelog = {"0.87.0": [], "0.10.0": []}
        assert audit_releases(self.TAGS, changelog, "0.88.0") == []

    def test_known_unreleased_baseline_is_grandfathered(self) -> None:
        changelog = {"0.87.0": [], "0.86.0": [], "0.85.0": []} | {
            v: [] for v in generate_changelog.KNOWN_UNRELEASED
        }
        assert audit_releases(self.TAGS, changelog, "0.88.0") == []

    def test_no_tags_reports_nothing(self) -> None:
        """An empty fetch means no window at all, not "everything is missing"."""
        assert audit_releases([], {"0.87.0": []}, "0.88.0") == []

    def test_results_sort_numerically_not_lexically(self) -> None:
        changelog = {"0.87.0": [], "0.100.0": [], "0.90.0": []}
        assert audit_releases(self.TAGS, changelog, "0.88.0") == ["0.90.0", "0.100.0"]

    def test_baseline_matches_the_versions_it_documents(self) -> None:
        """The frozen list must not grow -- a new entry means a missed release."""
        assert len(generate_changelog.KNOWN_UNRELEASED) == 18
