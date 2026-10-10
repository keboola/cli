"""Unit tests for the release-scope audit (``scripts/check_release_scope.py``).

The check exists because of a real miss in v0.91.0. PR #625 merged to ``main``
after the release PR had branched but before it merged, so it ended up INSIDE
the tag's tree and OUTSIDE the changelog entry the release notes are rendered
from. Nothing caught it: ``make changelog-check`` proves that every released
version has an entry, never that an entry covers every commit under the tag.
Tagging would have shipped a new plugin command with no release note.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_release_scope.py"
_spec = importlib.util.spec_from_file_location("check_release_scope", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_release_scope = importlib.util.module_from_spec(_spec)
sys.modules["check_release_scope"] = check_release_scope
_spec.loader.exec_module(check_release_scope)

merged_prs = check_release_scope.merged_pr_numbers
referenced_prs = check_release_scope.referenced_pr_numbers
missing = check_release_scope.missing_references


class TestMergedPrNumbers:
    """PR numbers come from the squash-merge subject GitHub writes."""

    def test_extracts_trailing_pr_reference(self) -> None:
        log = "b5d4be39 docs(plugin): polish workspace-load guidance (#698)\n"
        assert merged_prs(log) == ["698"]

    def test_ignores_a_commit_without_a_pr_reference(self) -> None:
        """Local commits on the release branch carry no ``(#N)`` and are not PRs."""
        log = "4523f6d3 chore(release): 0.91.0\n1e584ac0 fix: something (#42)\n"
        assert merged_prs(log) == ["42"]

    def test_only_a_trailing_reference_counts(self) -> None:
        """A ``(#N)`` mid-subject is a cross-reference, not this commit's PR."""
        log = "abc1234 fix(sync): follow-up to (#686) behaviour change\n"
        assert merged_prs(log) == []

    def test_handles_a_merge_commit_subject(self) -> None:
        log = "fc7ae7db Merge pull request #627 from keboola/feat/publish\n"
        assert merged_prs(log) == ["627"]

    def test_preserves_order_and_deduplicates(self) -> None:
        log = "a1 x (#5)\nb2 y (#7)\nc3 z (#5)\n"
        assert merged_prs(log) == ["5", "7"]


class TestReferencedPrNumbers:
    """Any ``#N`` anywhere in the release's changelog bullets counts as covered."""

    def test_reads_the_prefix_decoration(self) -> None:
        notes = ["New (#692): workspace load now clones."]
        assert referenced_prs(notes) == {"692"}

    def test_reads_a_multi_pr_decoration(self) -> None:
        notes = ["Fix (#686, #694, #696): push stamps API-derived baselines."]
        assert referenced_prs(notes) == {"686", "694", "696"}

    def test_reads_a_reference_from_mid_sentence(self) -> None:
        """Issue numbers cited in prose count too -- both are GitHub numbers."""
        notes = ["New: the write path fixes what the #600 audit finds."]
        assert referenced_prs(notes) == {"600"}


class TestMissingReferences:
    """The actual gate: every merged PR must appear in the new entry."""

    def test_fully_covered_release_reports_nothing(self) -> None:
        log = "a1 feat: x (#692)\nb2 fix: y (#694)\n"
        notes = ["New (#692): x.", "Fix (#694): y."]
        assert missing(log, notes, ignore=frozenset()) == []

    def test_reports_a_merged_pr_absent_from_the_notes(self) -> None:
        """This is the v0.91.0 / #625 miss, reproduced."""
        log = "a1 feat: setup (#625)\nb2 fix: y (#694)\n"
        notes = ["Fix (#694): y."]
        assert missing(log, notes, ignore=frozenset()) == ["625"]

    def test_ignored_pr_is_skipped(self) -> None:
        """The release PR itself is in the log once merged, never in its own notes."""
        log = "a1 chore(release): 0.91.0 (#699)\nb2 fix: y (#694)\n"
        notes = ["Fix (#694): y."]
        assert missing(log, notes, ignore=frozenset({"699"})) == []

    def test_reports_every_miss_not_just_the_first(self) -> None:
        log = "a1 x (#1)\nb2 y (#2)\nc3 z (#3)\n"
        assert missing(log, notes := ["Fix (#2): y."], ignore=frozenset()) == ["1", "3"]
        assert notes  # guard against the walrus being optimised away by a rewrite


class TestArmingAndFailOpen:
    """CI must arm this only on a release PR, and never block an ordinary one.

    The check needs tags and real history (``git log v<last>..HEAD``), which a
    default shallow CI checkout does not have. Deepening every PR run to buy a
    check that only matters on release PRs is the wrong trade, so the script
    decides whether it applies and degrades to a warning when git cannot answer.
    """

    def test_not_armed_when_the_version_is_unchanged(self) -> None:
        assert check_release_scope.should_arm(base="0.91.0", head="0.91.0") is False

    def test_not_armed_when_the_branch_trails_a_released_main(self) -> None:
        """A stale feature branch behind main must not look like a release PR."""
        assert check_release_scope.should_arm(base="0.91.0", head="0.90.1") is False

    def test_armed_when_the_pr_raises_the_version(self) -> None:
        assert check_release_scope.should_arm(base="0.90.1", head="0.91.0") is True

    def test_armed_for_a_pre_release_bump(self) -> None:
        assert check_release_scope.should_arm(base="0.90.1", head="0.91.0b1") is True

    def test_unreadable_base_does_not_arm(self) -> None:
        """Fail open: an unreadable base must never block an ordinary PR."""
        assert check_release_scope.should_arm(base="", head="0.91.0") is False

    def test_malformed_version_does_not_arm(self) -> None:
        assert check_release_scope.should_arm(base="not-a-version", head="0.91.0") is False
