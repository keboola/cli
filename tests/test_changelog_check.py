"""Unit tests for the changelog-coverage audit (``scripts/generate_changelog.py``).

Regression coverage for the `make changelog-check` pre-release handling: a
published pre-release tag (e.g. ``v0.44.0b1``) whose ``CHANGELOG`` entry lives
on an unmerged feature branch must NOT be reported as missing on ``main``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
