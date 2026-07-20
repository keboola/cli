"""Tests for scripts/gen_command_reference.py (release-asset reference generator)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "gen_command_reference", Path("scripts") / "gen_command_reference.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def reference() -> str:
    return _load_script().build_reference()


class TestGenCommandReference:
    def test_deterministic(self, reference: str) -> None:
        """Two runs produce byte-identical output (reviewable release-asset diffs)."""
        assert reference == _load_script().build_reference()

    def test_contains_every_visible_group_and_leaf(self, reference: str) -> None:
        """Cross-check against the command-sync walker: no visible command missing."""
        spec = importlib.util.spec_from_file_location(
            "check_command_sync", Path("scripts") / "check_command_sync.py"
        )
        assert spec is not None and spec.loader is not None
        sync = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sync)
        leaves, _groups = sync.collect_commands()
        missing = [p for p in leaves if f"### `kbagent {' '.join(p)}`" not in reference]
        assert missing == [], f"generated reference is missing {len(missing)} commands"

    def test_global_options_present(self, reference: str) -> None:
        for flag in ("--json", "--deny-writes", "--deny-destructive", "--allow-env-manage-token"):
            assert f"`{flag}`" in reference

    def test_required_flags_marked(self, reference: str) -> None:
        """job run's --project row carries the required marker."""
        section = reference.split("### `kbagent job run`", 1)[1].split("### ", 1)[0]
        project_row = next(line for line in section.splitlines() if "`--project`" in line)
        assert "| yes |" in project_row

    def test_hidden_alias_excluded(self, reference: str) -> None:
        """The hidden `sl` alias (and its subtree) is not documented."""
        assert "### `kbagent sl " not in reference
        assert "## `sl`" not in reference

    def test_help_option_excluded(self, reference: str) -> None:
        assert "`--help`" not in reference

    def test_header_carries_version(self, reference: str) -> None:
        from keboola_agent_cli import __version__

        assert f"Generated from kbagent v{__version__}" in reference
