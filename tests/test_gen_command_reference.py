"""Tests for scripts/gen_command_reference.py (release-asset reference generator)."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import click
import pytest


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "gen_command_reference", Path("scripts") / "gen_command_reference.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the script's @dataclass under
    # `from __future__ import annotations` resolves its module via
    # sys.modules[cls.__module__] at class-creation time.
    sys.modules[spec.name] = mod
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


# A value-taking option's metavar span: angle-bracketed, no whitespace inside.
# Choice values (e.g. `readOnly`) keep their literal case, so uppercase is allowed;
# the durable contract the connection-docs gate matches is only ``/^<.*>$/``.
_METAVAR_SPAN = re.compile(r"^<[A-Za-z0-9|_-]+>$")


class TestMetavarContract:
    """Issue #513: the metavar column is a published contract; pin its shape.

    These tests fail CI on a Typer/Click bump that changes ``make_metavar()``
    (e.g. reverting `<str>` back to bare `TEXT`), so the drift is caught here
    instead of silently breaking the downstream connection-docs freshness gate.
    """

    def test_scalar_types_map_to_documented_metavars(self) -> None:
        """Each scalar Click type renders as its stable lowercase `<...>` span."""
        stable = _load_script()._stable_option_metavar
        cases = {
            str: "<str>",
            int: "<int>",
            float: "<float>",
            click.Path(): "<path>",
            click.IntRange(0, 5): "<int>",
        }
        for click_type, expected in cases.items():
            option = click.Option(["--x"], type=click_type)
            assert stable(option) == expected

    def test_choice_preserves_literal_case(self) -> None:
        """Choice values are real CLI tokens -- their case must not be normalized."""
        stable = _load_script()._stable_option_metavar
        option = click.Option(["--role"], type=click.Choice(["admin", "readOnly", "share"]))
        assert stable(option) == "<admin|readOnly|share>"

    def test_explicit_metavar_is_wrapped_and_lowercased(self) -> None:
        stable = _load_script()._stable_option_metavar
        option = click.Option(["--alias"], metavar="ALIAS")
        assert stable(option) == "<alias>"

    def test_every_value_option_matches_the_span_contract(self) -> None:
        """Every scalar/choice rendering satisfies the downstream `<...>` matcher."""
        stable = _load_script()._stable_option_metavar
        for click_type in (str, int, float, click.Path(), click.Choice(["a", "b_c", "D"])):
            option = click.Option(["--x"], type=click_type)
            assert _METAVAR_SPAN.match(stable(option))

    def test_flags_carry_no_metavar_span(self, reference: str) -> None:
        """A boolean flag row must not gain a `<...>` value span."""
        section = reference.split("## Global options", 1)[1].split("\n## ", 1)[0]
        json_row = next(line for line in section.splitlines() if "`--json`" in line)
        assert "`<" not in json_row, f"flag row unexpectedly has a metavar: {json_row}"

    def test_no_bare_uppercase_metavar_on_option_rows(self, reference: str) -> None:
        """No option row may carry a bare `TEXT`/`INTEGER`/`PATH`/`FLOAT` metavar."""
        offenders = [
            line
            for line in reference.splitlines()
            if line.startswith("| `--") and re.search(r"`(TEXT|INTEGER|PATH|FLOAT|BOOLEAN)`", line)
        ]
        assert offenders == [], f"bare uppercase metavars leaked (Click drift?): {offenders[:3]}"

    def test_scalar_and_choice_metavars_documented(self, reference: str) -> None:
        """The documented scalar/choice forms are present in the generated asset."""
        for expected in ("`<str>`", "`<int>`", "`<path>`", "`<admin|guest|readOnly|share>`"):
            assert expected in reference, f"expected metavar {expected} missing from reference"
