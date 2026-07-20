"""Unit tests for release-notes generation (``scripts/gen_release_notes.py``).

The script is the release pipeline's source of GitHub Release bodies AND the
forward changelog gate (a stable tag without a ``CHANGELOG`` entry must fail
before anything publishes) — both behaviors are pinned here.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from keboola_agent_cli.commands.changelog import _PREFIX_RE

# ``scripts/`` is not an importable package; load the module by file path.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "gen_release_notes.py"
_spec = importlib.util.spec_from_file_location("gen_release_notes", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
gen_release_notes = importlib.util.module_from_spec(_spec)
sys.modules["gen_release_notes"] = gen_release_notes
_spec.loader.exec_module(gen_release_notes)


collect_versions = gen_release_notes.collect_versions


def render_notes(versions: list[str], changelog: dict[str, list[str]]) -> str:
    return gen_release_notes.render_notes(versions, changelog, _PREFIX_RE)


# Newest-first, mirroring the real CHANGELOG dict's documented ordering.
_CHANGELOG = {
    "0.71.0": ["New (web UI): repartition tab.", "The serve endpoint forwards new fields."],
    "0.70.1": ["Fix: config.json hardening.", "Note: clearer not-found errors."],
    "0.70.0": ["BREAKING: removed git-branches."],
    "0.66.1": ["Fix (#479): schedules activate."],
}


class TestCollectVersions:
    def test_adjacent_released_version_yields_single_section(self) -> None:
        assert collect_versions("0.71.0", {"0.70.1", "0.66.1"}, _CHANGELOG) == ["0.71.0"]

    def test_catch_up_walk_stops_at_first_released(self) -> None:
        assert collect_versions("0.71.0", {"0.66.1"}, _CHANGELOG) == [
            "0.71.0",
            "0.70.1",
            "0.70.0",
        ]

    def test_version_not_in_changelog_returns_empty(self) -> None:
        assert collect_versions("9.9.9", {"0.66.1"}, _CHANGELOG) == []

    def test_version_itself_released_is_still_included(self) -> None:
        # A pre-created release (or a pipeline re-run) lists the version being
        # released among the released set; it must not stop the walk at itself.
        assert collect_versions("0.71.0", {"0.71.0", "0.66.1"}, _CHANGELOG) == [
            "0.71.0",
            "0.70.1",
            "0.70.0",
        ]

    def test_old_line_hotfix_ignores_newer_releases(self) -> None:
        # Tagging 0.70.0 while 0.71.0 is already out: newer releases are
        # irrelevant, the walk stops at the first OLDER released version.
        assert collect_versions("0.70.0", {"0.71.0", "0.66.1"}, _CHANGELOG) == ["0.70.0"]

    def test_no_released_boundary_falls_back_to_single(self) -> None:
        # Empty or changelog-disjoint released set: the boundary is unknowable,
        # so never emit the entire history.
        assert collect_versions("0.71.0", set(), _CHANGELOG) == ["0.71.0"]
        assert collect_versions("0.71.0", {"0.50.0"}, _CHANGELOG) == ["0.71.0"]


class TestRenderNotes:
    def test_single_version_section_and_verbatim_entry(self) -> None:
        notes = render_notes(["0.70.0"], _CHANGELOG)
        assert "### v0.70.0" in notes
        assert "removed git-branches." in notes
        assert "Catch-up release" not in notes

    def test_recognised_prefix_is_bolded(self) -> None:
        notes = render_notes(["0.70.1"], _CHANGELOG)
        assert "- **Fix:** config.json hardening." in notes
        assert "- **Note:** clearer not-found errors." in notes

    def test_prefix_with_issue_reference_is_bolded(self) -> None:
        notes = render_notes(["0.66.1"], _CHANGELOG)
        assert "- **Fix (#479):** schedules activate." in notes

    def test_prefix_with_freeform_decoration_is_bolded(self) -> None:
        # `_PREFIX_RE` allows any parenthesized decoration, e.g. "New (web UI):".
        notes = render_notes(["0.71.0"], _CHANGELOG)
        assert "- **New (web UI):** repartition tab." in notes

    def test_unprefixed_entry_stays_verbatim(self) -> None:
        notes = render_notes(["0.71.0"], _CHANGELOG)
        assert "- The serve endpoint forwards new fields." in notes

    def test_catch_up_release_lists_range_and_all_sections(self) -> None:
        notes = render_notes(["0.71.0", "0.70.1", "0.70.0"], _CHANGELOG)
        assert "previously untagged versions v0.70.0 through v0.71.0" in notes
        for version in ("v0.71.0", "v0.70.1", "v0.70.0"):
            assert f"### {version}" in notes

    def test_footer_links_the_changelog(self) -> None:
        notes = render_notes(["0.71.0"], _CHANGELOG)
        assert "kbagent changelog --full" in notes
        assert "changelog.py" in notes


class TestCli:
    """End-to-end runs against the real CHANGELOG (the package is installed)."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(_SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
        )

    def test_known_version_prints_notes(self) -> None:
        result = self._run("--version", "0.66.1")
        assert result.returncode == 0
        assert "### v0.66.1" in result.stdout

    def test_released_flag_bounds_the_walk(self) -> None:
        result = self._run("--version", "0.66.1", "--released", "0.66.0")
        assert result.returncode == 0
        assert "### v0.66.1" in result.stdout
        assert "### v0.66.0" not in result.stdout

    def test_missing_stable_version_fails(self) -> None:
        result = self._run("--version", "999.0.0")
        assert result.returncode == 1
        assert "no CHANGELOG entry for 999.0.0" in result.stderr

    def test_missing_with_allow_missing_writes_nothing(self, tmp_path: Path) -> None:
        out = tmp_path / "notes.md"
        result = self._run("--version", "999.0.0b1", "--allow-missing", "--output", str(out))
        assert result.returncode == 0
        assert not out.exists()

    def test_output_file_written(self, tmp_path: Path) -> None:
        out = tmp_path / "notes.md"
        result = self._run("--version", "0.66.1", "--output", str(out))
        assert result.returncode == 0
        assert "### v0.66.1" in out.read_text(encoding="utf-8")
