"""Tests for scripts/gen_command_reference.py (release-asset reference generator)."""

from __future__ import annotations

import enum
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


class TestValuelessOptions:
    """Options that consume no value must never grow a metavar span.

    A ``count=True`` counter is the trap: Click leaves ``is_flag`` False on it,
    so keying only on ``is_flag`` publishes ``--verbose <int>`` and makes the
    downstream connection-docs gate demand a value the CLI does not accept.
    """

    def test_count_option_gets_no_metavar(self) -> None:
        mod = _load_script()
        option = click.Option(["--verbose", "-v"], count=True, help="Increase verbosity")
        assert option.is_flag is False and option.count is True  # the trap, made explicit
        row = mod._format_param(option, click.Context(click.Command("x")))
        assert row == "| `--verbose` / `-v` |  | Increase verbosity |"
        assert "<" not in row

    def test_boolean_flag_gets_no_metavar(self) -> None:
        mod = _load_script()
        option = click.Option(["--json"], is_flag=True, help="JSON output")
        row = mod._format_param(option, click.Context(click.Command("x")))
        assert row == "| `--json` |  | JSON output |"

    def test_value_option_still_gets_a_metavar(self) -> None:
        """Guard the opposite mistake: the suppression must stay narrow."""
        mod = _load_script()
        option = click.Option(["--limit"], type=int, help="Max rows")
        row = mod._format_param(option, click.Context(click.Command("x")))
        assert row == "| `--limit` `<int>` |  | Max rows |"


class TestCompositeAndUnknownTypes:
    """Composite/custom types must degrade to a well-formed span, never a broken one."""

    def test_tuple_type_renders_member_types(self) -> None:
        """``click.Tuple``'s own name is ``<text integer>`` -- naive use nests brackets."""
        mod = _load_script()
        ptype = click.Tuple([click.STRING, click.INT])
        assert ptype.name == "<text integer>"  # the shape that must not leak through
        option = click.Option(["--pair"], type=ptype)
        assert mod._stable_option_metavar(option) == "<str,int>"

    def test_multi_value_nargs_repeats_the_token(self) -> None:
        mod = _load_script()
        option = click.Option(["--point"], type=int, nargs=2)
        assert mod._stable_option_metavar(option) == "<int,int>"

    def test_multiple_does_not_change_the_span(self) -> None:
        """`multiple=True` repeats the OPTION, not its value -- span stays scalar."""
        mod = _load_script()
        option = click.Option(["--tag"], type=str, multiple=True)
        assert mod._stable_option_metavar(option) == "<str>"

    def test_nameless_custom_type_falls_back_to_value(self) -> None:
        """A custom ParamType with no `name` must not be mislabelled as `<str>`."""
        mod = _load_script()

        class NamelessType(click.ParamType):
            pass

        assert getattr(NamelessType(), "name", None) is None
        option = click.Option(["--x"], type=NamelessType())
        assert mod._stable_option_metavar(option) == "<value>"

    def test_unknown_named_type_is_sanitized(self) -> None:
        """An unmapped type name keeps its identity but stays inside the alphabet."""
        mod = _load_script()

        class WeirdType(click.ParamType):
            name = "my weird <type>"

        option = click.Option(["--x"], type=WeirdType())
        assert mod._stable_option_metavar(option) == "<my-weird-type>"


class TestEnumBackedChoice:
    """`click.Choice(SomeEnum)` parses member NAMES; `str(member)` is `Color.RED`."""

    def test_enum_choice_renders_member_names(self) -> None:
        mod = _load_script()

        class Color(enum.Enum):
            RED = "r"
            BLUE = "b"

        option = click.Option(["--color"], type=click.Choice(Color))
        assert mod._stable_option_metavar(option) == "<RED|BLUE>"

    def test_string_choice_case_is_never_normalized(self) -> None:
        """Even a case-insensitive Choice keeps the literal tokens users type."""
        mod = _load_script()
        option = click.Option(
            ["--role"], type=click.Choice(["admin", "readOnly"], case_sensitive=False)
        )
        assert mod._stable_option_metavar(option) == "<admin|readOnly>"


# One option cell of a generated row: backticked option names, then an optional
# `<...>` metavar span. This is the grammar the connection-docs gate parses.
_OPTION_CELL = re.compile(
    r"^(?P<names>`-{1,2}[^`]+`(?: / `-{1,2}[^`]+`)*)(?: `(?P<metavar><[^`]+>)`)?$"
)
_STABLE_SPAN = re.compile(r"^<[A-Za-z0-9][A-Za-z0-9|,_-]*>$")


class TestLiveCommandTreeGrammar:
    """Walk the REAL Typer tree: the synthetic cases above use plain `click`,
    while the app renders Typer's vendored Click objects. This is the test that
    actually catches vendored-Click drift.
    """

    def test_every_emitted_option_row_matches_the_grammar(self, reference: str) -> None:
        rows = [line for line in reference.splitlines() if line.startswith("| `-")]
        # Guard against a silently-empty scan passing this test.
        assert len(rows) > 500, f"only {len(rows)} option rows scanned -- walker broken?"
        malformed: list[str] = []
        for row in rows:
            cell = row.split(" | ", 1)[0].removeprefix("| ").strip()
            match = _OPTION_CELL.match(cell)
            if match is None:
                malformed.append(row)
                continue
            span = match.group("metavar")
            if span is not None and not _STABLE_SPAN.match(span):
                malformed.append(row)
        assert malformed == [], f"{len(malformed)} option rows off-contract: {malformed[:3]}"

    def test_no_live_option_row_nests_angle_brackets(self, reference: str) -> None:
        """`<<text-integer>>` (the naive composite rendering) must never appear."""
        assert "<<" not in reference and ">>" not in reference

    def test_live_tree_has_no_valueless_option_with_a_span(self, reference: str) -> None:
        """Cross-check the rendered rows against the live params themselves."""
        mod = _load_script()
        import typer.main

        from keboola_agent_cli.cli import app
        from keboola_agent_cli.commands.repl import _is_group

        click_app = typer.main.get_command(app)
        # TypeGuard narrowing: Typer >=0.25 vendors Click, so the returned object
        # is not an instance of the plain `click` classes (see the script header).
        assert _is_group(click_app)
        offenders: list[str] = []
        with click.Context(click_app, info_name=mod.PROG) as root_ctx:
            leaves = [
                *(mod.LeafCommand(path=(), command=click_app, ctx=root_ctx),),
                *mod._walk(click_app, root_ctx),
            ]
            for leaf in leaves:
                for param in leaf.command.params:
                    if getattr(param, "param_type_name", "") != "option":
                        continue
                    row = mod._format_param(param, leaf.ctx)
                    if row is None:
                        continue
                    takes_value = mod._takes_a_value(param)
                    if takes_value != ("`<" in row):
                        offenders.append(f"{' '.join(leaf.path)}: {row}")
        assert offenders == [], f"metavar presence disagrees with value-taking: {offenders[:3]}"
