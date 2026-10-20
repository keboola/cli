"""Tests for scripts/check_cli_spec.py -- the `make cli-spec-check` gate.

They pin five things:

  1. The committed repository passes: the baseline and docs/cli-manifest.json
     match the live tree.
  2. The walk of the check sees the same commands as
     scripts/check_command_sync.py (proposal section 7.4), so the two cannot
     drift apart.
  3. The ratchet fails on a new violation, a stale baseline entry, an unknown
     rule, growth against the base branch and a loosened blocking flag; a
     non-blocking rule never fails.
  4. Single rules find what they are for on synthetic input.
  5. docs/cli-interface-spec.md has an anchor for every rule id, and
     CLAUDE.md, CONTRIBUTING.md and the reviewer playbook link it (spec C7).
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

if importlib.util.find_spec("fastapi") is None:  # pragma: no cover
    pytest.skip("FastAPI not installed; run `uv sync --extra server`", allow_module_level=True)

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"


def _load(name: str, filename: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spec_check = _load("_check_cli_spec_under_test", "check_cli_spec.py")
command_sync = _load("_check_command_sync_for_spec_test", "check_command_sync.py")


@pytest.fixture(scope="module")
def tree() -> Any:
    return spec_check.load_tree()


@pytest.fixture(scope="module")
def results(tree: Any) -> dict[str, dict[str, str]]:
    return spec_check.evaluate(tree)


@pytest.fixture(scope="module")
def baseline() -> dict[str, Any]:
    return json.loads(spec_check.BASELINE_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 1. The committed repository passes
# --------------------------------------------------------------------------


def test_live_tree_passes_against_committed_baseline(
    results: dict[str, dict[str, str]], baseline: dict[str, Any]
) -> None:
    assert spec_check.ratchet(results, baseline) == []


def test_committed_manifest_is_fresh(tree: Any) -> None:
    assert spec_check.MANIFEST_PATH.read_text(encoding="utf-8") == spec_check.manifest_text(tree)


def test_baseline_is_what_write_baseline_produces(
    results: dict[str, dict[str, str]], baseline: dict[str, Any]
) -> None:
    """Sorted, one subject per line, nothing that `make cli-spec-baseline` would change."""
    assert spec_check.build_baseline(results, baseline) == baseline


def test_every_rule_has_a_blocking_flag(baseline: dict[str, Any]) -> None:
    assert set(baseline["blocking"]) == {r.id for r in spec_check.RULES}


# --------------------------------------------------------------------------
# 2. One walk
# --------------------------------------------------------------------------


def test_leaf_set_equals_check_command_sync(tree: Any) -> None:
    leaves, _ = command_sync.collect_commands()
    assert {lf.path for lf in spec_check.visible_leaves(tree)} == set(leaves)


def test_aliases_are_found_and_skipped(tree: Any) -> None:
    assert tree.aliases == {"mr": "merge-request", "sl": "semantic-layer"}
    assert not any(lf.path[0] in tree.aliases for lf in tree.leaves)


def test_rule_ids_are_unique_and_disjoint() -> None:
    evaluated = [r.id for r in spec_check.RULES]
    assert len(evaluated) == len(set(evaluated))
    contract, deferred = set(spec_check.CONTRACT_RULES), set(spec_check.DEFERRED_RULES)
    assert not (set(evaluated) & contract)
    assert not (set(evaluated) & deferred)
    assert not (contract & deferred)


def _spec_text() -> str:
    return (spec_check.REPO_ROOT / spec_check.SPEC_DOC).read_text(encoding="utf-8")


def test_every_rule_id_has_one_spec_anchor() -> None:
    """Failure lines link to `docs/cli-interface-spec.md#<rule-id>` (spec C7)."""
    found = Counter(re.findall(r'<a id="([^"]+)"></a>', _spec_text()))
    assert [a for a, n in found.items() if n > 1] == []
    rule_ids = (
        {r.id for r in spec_check.RULES}
        | set(spec_check.CONTRACT_RULES)
        | set(spec_check.DEFERRED_RULES)
    )
    assert rule_ids - set(found) == set()
    assert {a for a in found if "." in a} - rule_ids == set()


_NONE = "\u2013"  # en dash: the index cell of a rule without a level or version
_RULE_HEADING = re.compile(r"^\*\*([A-Z]+-?\d+)(?: · (.+?))?(?: · (.+?))?\.\*\*", re.MULTILINE)
_INDEX_ROW = re.compile(r"^\| \[([A-Z]+-?\d+)\]\(#\1\) \| (.+?) \| (.+?) \| .+ \|$", re.MULTILINE)


def test_rule_index_matches_the_rule_headings() -> None:
    """The rule index lists every rule once, with the level and version of its heading."""
    text = _spec_text()
    headings = [
        (rid, "withdrawn" if rid == "T7" else (level or _NONE), applies or _NONE)
        for rid, level, applies in _RULE_HEADING.findall(text)
    ]
    assert len(headings) == len({rid for rid, _, _ in headings})
    assert _INDEX_ROW.findall(text) == headings


def test_short_flag_table_matches_the_registries() -> None:
    rows = re.findall(
        r"^\| `(-[A-Za-z])` \| (.+?) \| (fixed|global|reserved) \|$", _spec_text(), re.MULTILINE
    )
    fixed = {flag: long.strip("`") for flag, long, status in rows if status != "reserved"}
    reserved = {flag for flag, _, status in rows if status == "reserved"}
    assert fixed == spec_check.SHORT_FLAGS
    assert reserved == spec_check.RESERVED_SHORT_FLAGS


def test_a_new_use_of_a_reserved_short_flag_fails() -> None:
    option = SimpleNamespace(
        param_type_name="option", name="name", opts=["--name", "-n"], secondary_opts=[]
    )
    leaf = spec_check.Leaf(
        path=("demo",), category="read", params=(option,), callback=None, help="Demo.", hidden=False
    )
    tree = spec_check.Tree(root=None, leaves=[leaf], groups=[])
    found = spec_check.r_short_one_meaning(spec_check.Context(tree))
    assert found == {"demo -n": "-n is a reserved short flag (no new use)"}
    rule = "args.short-flag-one-meaning"
    fails = spec_check.ratchet(_results(**{rule: found}), _small_baseline())
    assert len(fails) == 1 and fails[0].startswith(f"FAIL new      {rule}: demo -n")


@pytest.mark.parametrize(
    "path", ["CLAUDE.md", "CONTRIBUTING.md", "plugins/kbagent/agents/kbagent-pr-reviewer.md"]
)
def test_binding_documents_name_the_spec(path: str) -> None:
    """The spec is binding through these three files (spec C7)."""
    text = (spec_check.REPO_ROOT / path).read_text(encoding="utf-8")
    assert spec_check.SPEC_DOC in text


# --------------------------------------------------------------------------
# 3. The ratchet
# --------------------------------------------------------------------------

RULE = "safety.destructive-has-yes"
SOFT = "safety.write-has-dry-run"


def _small_baseline() -> dict[str, Any]:
    blocking = {r.id: True for r in spec_check.RULES}
    blocking[SOFT] = False
    return {
        "blocking": blocking,
        "violations": {RULE: ["branch delete"], SOFT: ["a write"]},
        "exceptions": {RULE: {"config delete": "soft delete"}},
    }


def _results(**overrides: dict[str, str]) -> dict[str, dict[str, str]]:
    found: dict[str, dict[str, str]] = {r.id: {} for r in spec_check.RULES}
    found[RULE] = {"branch delete": "x", "config delete": "x"}
    found[SOFT] = {"a write": "x"}
    found.update(overrides)
    return found


def test_unchanged_baseline_passes() -> None:
    assert spec_check.ratchet(_results(), _small_baseline()) == []


def test_new_violation_fails() -> None:
    fails = spec_check.ratchet(
        _results(**{RULE: {"branch delete": "x", "config delete": "x", "new cmd": "x"}}),
        _small_baseline(),
    )
    assert len(fails) == 1
    assert fails[0].startswith(f"FAIL new      {RULE}: new cmd")
    assert f"docs/cli-interface-spec.md#{RULE}" in fails[0]
    assert "rule: category destructive => --yes" in fails[0]


def test_stale_entry_fails() -> None:
    fails = spec_check.ratchet(_results(**{RULE: {"config delete": "x"}}), _small_baseline())
    assert fails == [
        (
            f"FAIL stale    {RULE}: branch delete -- no longer a violation; "
            "remove it from the baseline ('make cli-spec-baseline')"
        )
    ]


def test_stale_exception_fails() -> None:
    fails = spec_check.ratchet(_results(**{RULE: {"branch delete": "x"}}), _small_baseline())
    assert len(fails) == 1 and "config delete" in fails[0]


def test_non_blocking_rule_only_notes(capsys: pytest.CaptureFixture[str]) -> None:
    fails = spec_check.ratchet(_results(**{SOFT: {"another write": "x"}}), _small_baseline())
    assert fails == []
    out = capsys.readouterr().out
    assert f"NOTE new      {SOFT}: another write" in out
    assert f"NOTE stale    {SOFT}: a write" in out


def test_unknown_rule_and_missing_flag_fail() -> None:
    baseline = _small_baseline()
    baseline["violations"]["no.such-rule"] = ["x"]
    del baseline["blocking"][RULE]
    fails = spec_check.ratchet(_results(), baseline)
    assert any("FAIL unknown   no.such-rule" in f for f in fails)
    assert any(f"FAIL unknown   {RULE}: the baseline has no blocking flag" in f for f in fails)


def test_growth_against_base_fails_for_blocking_rules_only() -> None:
    base = _small_baseline()
    base["violations"] = {}
    fails = spec_check.ratchet(_results(), _small_baseline(), base)
    assert fails == [f"FAIL grown    {RULE}: 0 -> 1 entries -- the baseline gained entries"]


def test_new_rule_starts_its_baseline_without_a_growth_failure() -> None:
    """A rule that the base baseline does not know records its first entries."""
    base = _small_baseline()
    del base["blocking"][RULE]
    del base["violations"][RULE]
    del base["exceptions"][RULE]
    head = _small_baseline()
    head["exceptions"][RULE] = {}
    found = _results(**{RULE: {"branch delete": "x", "config delete": "x"}})
    head["violations"][RULE] = ["branch delete", "config delete"]
    assert spec_check.ratchet(found, head, base) == []


def test_new_rule_still_needs_the_label_for_an_exception() -> None:
    base = _small_baseline()
    del base["blocking"][RULE]
    del base["violations"][RULE]
    del base["exceptions"][RULE]
    fails = spec_check.ratchet(_results(), _small_baseline(), base)
    assert fails == [
        (
            f"FAIL exception {RULE}: config delete -- new exception; a reviewer adds the "
            "'cli-spec-exception' label to the pull request to accept it"
        )
    ]


def test_existing_rule_that_grows_still_fails() -> None:
    head = _small_baseline()
    head["violations"][RULE] = ["branch delete", "workspace delete"]
    found = _results(
        **{RULE: {"branch delete": "x", "config delete": "x", "workspace delete": "x"}}
    )
    assert spec_check.ratchet(found, head, _small_baseline()) == [
        f"FAIL grown    {RULE}: 1 -> 2 entries -- the baseline gained entries"
    ]


def test_renamed_command_in_a_tree_rule_is_no_growth() -> None:
    """A rename (or a hidden alias that keeps the old name, C3) changes the subject only."""
    head = _small_baseline()
    head["violations"][RULE] = ["branch remove"]
    renamed = _results(**{RULE: {"branch remove": "x", "config delete": "x"}})
    assert spec_check.ratchet(renamed, head, _small_baseline()) == []


def test_new_exception_fails_without_the_flag() -> None:
    head = _small_baseline()
    head["exceptions"][RULE]["workspace delete"] = "reviewed"
    found = _results(
        **{RULE: {"branch delete": "x", "config delete": "x", "workspace delete": "x"}}
    )
    assert spec_check.ratchet(found, head, _small_baseline()) == [
        (
            f"FAIL exception {RULE}: workspace delete -- new exception; a reviewer adds the "
            "'cli-spec-exception' label to the pull request to accept it"
        )
    ]
    assert spec_check.ratchet(found, head, _small_baseline(), allow_new_exceptions=True) == []


def test_moving_a_violation_into_exceptions_fails() -> None:
    head = _small_baseline()
    head["violations"][RULE] = []
    head["exceptions"][RULE]["branch delete"] = "now an exception"
    fails = spec_check.ratchet(_results(), head, _small_baseline())
    assert fails == [
        (
            f"FAIL exception {RULE}: branch delete -- new exception; a reviewer adds the "
            "'cli-spec-exception' label to the pull request to accept it"
        )
    ]


def test_rule_removed_together_with_its_baseline_fails() -> None:
    base = _small_baseline()
    base["blocking"]["safety.retired-rule"] = True
    fails = spec_check.ratchet(_results(), _small_baseline(), base)
    assert fails == ["FAIL removed  safety.retired-rule: a rule of the base baseline is missing"]


def test_growth_of_a_source_rule_is_counted_not_named() -> None:
    """A function moved to another module renames its subject; that is no growth."""
    source_rule = "output.no-direct-print"
    base = _small_baseline()
    base["violations"][source_rule] = ["commands/data_app.py:deploy typer.echo"]
    head = _small_baseline()
    head["violations"][source_rule] = ["commands/_data_app_deploy.py:deploy typer.echo"]
    moved = _results(**{source_rule: {"commands/_data_app_deploy.py:deploy typer.echo": "x"}})
    assert spec_check.ratchet(moved, head, base) == []

    head["violations"][source_rule].append("commands/flow.py:flow_new print")
    moved[source_rule]["commands/flow.py:flow_new print"] = "x"
    assert spec_check.ratchet(moved, head, base) == [
        f"FAIL grown    {source_rule}: 1 -> 2 entries -- the baseline gained entries"
    ]


def test_loosened_blocking_flag_fails() -> None:
    head = _small_baseline()
    head["blocking"][RULE] = False
    fails = spec_check.ratchet(_results(), head, _small_baseline())
    assert fails == [f"FAIL loosened {RULE}: changed from blocking to non-blocking"]


def test_manifest_rule_takes_no_baseline() -> None:
    baseline = _small_baseline()
    baseline["violations"]["help.manifest-fresh"] = ["docs/cli-manifest.json"]
    stale = {"docs/cli-manifest.json": "differs"}
    fails = spec_check.ratchet(_results(**{"help.manifest-fresh": stale}), baseline)
    assert any(f.startswith("FAIL baseline  help.manifest-fresh") for f in fails)
    assert any(f.startswith("FAIL new      help.manifest-fresh") for f in fails)


def test_stale_manifest_is_a_violation(
    tree: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "cli-manifest.json"
    manifest.write_text(
        spec_check.manifest_text(tree).replace("Scheduled agent", "Agent"), encoding="utf-8"
    )
    monkeypatch.setattr(spec_check, "MANIFEST_PATH", manifest)
    assert spec_check.r_manifest_fresh(spec_check.Context(tree)) == {
        "docs/cli-manifest.json": "differs from the live tree: agent; run 'make cli-manifest'"
    }


def test_committed_context_commands_are_fresh() -> None:
    """AG1: the command part of `kbagent context` is the text generated from the manifest."""
    manifest = json.loads(spec_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    have = spec_check.CONTEXT_COMMANDS_PATH.read_text(encoding="utf-8")
    assert have == spec_check.context_commands_text(manifest)


def test_stale_context_commands_are_a_violation(
    tree: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = tmp_path / "_context_commands.py"
    text = spec_check.CONTEXT_COMMANDS_PATH.read_text(encoding="utf-8")
    module.write_text(text.replace("kbagent storage buckets", "kbagent storage bucketz"))
    monkeypatch.setattr(spec_check, "CONTEXT_COMMANDS_PATH", module)
    assert spec_check.r_context_from_manifest(spec_check.Context(tree)) == {
        spec_check.CONTEXT_COMMANDS_REL: "differs from the text generated from "
        "docs/cli-manifest.json; run 'make cli-manifest'"
    }


_SMALL_MANIFEST: dict[str, Any] = {
    "aliases": {"g": "grp"},
    "commands": [
        {"path": "grp", "kind": "group", "hidden": False, "help": "The group.\n\nMore text."},
        {
            "path": "grp do",
            "kind": "command",
            "hidden": False,
            "category": "write",
            "help": "Do it.\n\n\bDetails that stay in --help.",
            "params": [
                {
                    "kind": "option",
                    "name": "project",
                    "opts": ["--project"],
                    "type": "text",
                    "required": True,
                    "default": None,
                },
                {
                    "kind": "option",
                    "name": "limit",
                    "opts": ["--limit"],
                    "type": "integer",
                    "default": 10,
                },
                {
                    "kind": "option",
                    "name": "wait",
                    "opts": ["--wait"],
                    "secondary_opts": ["--no-wait"],
                    "type": "boolean",
                    "is_flag": True,
                    "default": True,
                },
                {
                    "kind": "option",
                    "name": "mode",
                    "opts": ["--mode"],
                    "type": "choice",
                    "choices": ["a", "b"],
                    "multiple": True,
                    "default": None,
                },
                {
                    "kind": "option",
                    "name": "old",
                    "opts": ["--old"],
                    "type": "text",
                    "hidden": True,
                    "default": None,
                },
                {
                    "kind": "argument",
                    "name": "name",
                    "opts": ["name"],
                    "type": "text",
                    "default": None,
                },
            ],
        },
        {
            "path": "solo",
            "kind": "command",
            "hidden": False,
            "category": "read",
            "help": 'Path "C:\\x" in """quotes""".',
            "params": [],
        },
    ],
}


def test_context_section_has_usage_and_first_help_paragraph() -> None:
    """Required options bare, optional in brackets, hidden ones left out. Wrapped at 78."""
    assert spec_check.context_sections(_SMALL_MANIFEST)["grp"] == (
        "### grp\n\nThe group.\n\n"
        "  kbagent grp do --project PROJECT [--limit N] [--wait/--no-wait]\n"
        "      [--mode a|b ...] [NAME]\n"
        "    Do it.\n"
    )


def test_context_commands_text_is_python_with_the_same_sections(tmp_path: Path) -> None:
    """Backslashes and triple quotes in a help text survive the generated module."""
    path = tmp_path / "_context_commands.py"
    path.write_text(spec_check.context_commands_text(_SMALL_MANIFEST), encoding="utf-8")
    module = _load("_context_commands_under_test", str(path))
    assert spec_check.context_sections(_SMALL_MANIFEST) == module.COMMAND_SECTIONS
    assert module.ALIASES == {"g": "grp"}


def test_details_of_an_unknown_rule_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert spec_check.main(["--details", "no.such-rule"]) == 2
    assert "unknown rule 'no.such-rule'" in capsys.readouterr().err


def test_build_baseline_keeps_flags_and_drops_fixed_exceptions() -> None:
    old = _small_baseline()
    new = spec_check.build_baseline(_results(**{RULE: {"branch delete": "x"}}), old)
    assert new["blocking"] == old["blocking"]
    assert new["violations"][RULE] == ["branch delete"]
    assert RULE not in new["exceptions"]


# --------------------------------------------------------------------------
# 4. Single rules on synthetic input
# --------------------------------------------------------------------------


def _context_with_source(tree: Any, source: str) -> Any:
    ctx = spec_check.Context(tree)
    calls = list(spec_check._calls("commands/demo.py", ast.parse(source)))
    ctx._cache["command_calls"] = calls
    ctx._cache["package_calls"] = calls
    return ctx


def test_no_direct_print_finds_each_kind(tree: Any) -> None:
    source = (
        "import sys, typer\n"
        "from rich.console import Console\n"
        "def cmd():\n"
        "    print('a')\n"
        "    typer.echo('b')\n"
        "    sys.stdout.write('c')\n"
        "    Console().print('d')\n"
        "def fine(formatter):\n"
        "    formatter.console.print('e')\n"
    )
    found = spec_check.r_no_direct_print(_context_with_source(tree, source))
    assert set(found) == {
        "commands/demo.py:cmd print",
        "commands/demo.py:cmd typer.echo",
        "commands/demo.py:cmd sys.stdout.write",
        "commands/demo.py:cmd Console()",
    }


def test_forward_retryable_and_single_parser(tree: Any) -> None:
    source = (
        "def cmd(formatter, exc, raw):\n"
        "    formatter.error(message='m', error_code=exc.error_code)\n"
        "    formatter.error(message='m', error_code=exc.error_code, retryable=exc.retryable)\n"
        "    raw.startswith('@')\n"
        "def parse_json_arg(raw):\n"
        "    raw.startswith('@')\n"
    )
    ctx = _context_with_source(tree, source)
    assert set(spec_check.r_forward_retryable(ctx)) == {"commands/demo.py:cmd"}
    assert set(spec_check.r_single_parser(ctx)) == {"commands/demo.py:cmd"}


def test_config_error_keeps_code(tree: Any) -> None:
    source = (
        "def replaces(formatter):\n"
        "    try:\n"
        "        pass\n"
        "    except ConfigError as exc:\n"
        "        formatter.error(message=exc.message, error_code=ErrorCode.CONFIG_ERROR)\n"
        "def keeps(formatter):\n"
        "    try:\n"
        "        pass\n"
        "    except (KeyError, ConfigError) as exc:\n"
        "        formatter.error(message=exc.message, error_code=exc.error_code)\n"
        "def other_handler(formatter):\n"
        "    try:\n"
        "        pass\n"
        "    except KeyError:\n"
        "        formatter.error(message='m', error_code=ErrorCode.CONFIG_ERROR)\n"
    )
    found = spec_check.r_config_error_keeps_code(_context_with_source(tree, source))
    assert set(found) == {"commands/demo.py:replaces"}


def test_ok_then_exit_declares_exit_code(tree: Any) -> None:
    source = (
        "import typer\n"
        "def exits_after_output(formatter, failed):\n"
        "    formatter.output({})\n"
        "    if failed:\n"
        "        raise typer.Exit(code=1)\n"
        "def declares(formatter, code):\n"
        "    formatter.output({}, exit_code=code)\n"
        "    if code:\n"
        "        raise typer.Exit(code=code)\n"
        "def returns_before_exit(formatter, dry_run):\n"
        "    if dry_run:\n"
        "        formatter.output({})\n"
        "        return\n"
        "    raise typer.Exit(code=5)\n"
        "def exits_zero(formatter):\n"
        "    formatter.output({})\n"
        "    raise typer.Exit(code=0)\n"
        "def exit_before_output(formatter, bad):\n"
        "    if bad:\n"
        "        raise typer.Exit(code=2)\n"
        "    formatter.output({})\n"
        "def other_receiver(fmt):\n"
        "    fmt.output({})\n"
        "    raise typer.Exit(code=1)\n"
        "def exit_in_json_branch(formatter):\n"
        "    if formatter.json_mode:\n"
        "        formatter.output({})\n"
        "    else:\n"
        "        pass\n"
        "    raise typer.Exit(1)\n"
    )
    ctx = spec_check.Context(tree)
    ctx._cache["command_modules"] = [("commands/demo.py", ast.parse(source))]
    assert set(spec_check.r_ok_then_exit(ctx)) == {
        "commands/demo.py:exits_after_output",
        "commands/demo.py:other_receiver",
        "commands/demo.py:exit_in_json_branch",
    }


def _demo_command(used: str, unused: str) -> None:
    print(used)


def test_no_dead_parameter() -> None:
    def option(name: str) -> SimpleNamespace:
        return SimpleNamespace(name=name, opts=[f"--{name}"], secondary_opts=[])

    leaf = spec_check.Leaf(
        path=("demo",),
        category="read",
        params=(option("used"), option("unused")),
        callback=_demo_command,
        help="Demo.",
        hidden=False,
    )
    tree = spec_check.Tree(root=None, leaves=[leaf], groups=[])
    found = spec_check.r_dead_parameter(spec_check.Context(tree))
    assert found == {"demo --unused": "parameter 'unused' is never read"}


def test_confirm_via_helper(tree: Any) -> None:
    source = (
        "import typer\n"
        "def cmd():\n"
        "    typer.confirm('sure?')\n"
        "def _confirm_or_abort():\n"
        "    typer.confirm('sure?')\n"
    )
    found = spec_check.r_confirm_via_helper(_context_with_source(tree, source))
    # Only the shared helper in commands/_helpers.py may ask: a copy of it is flagged too.
    assert set(found) == {"commands/demo.py:cmd", "commands/demo.py:_confirm_or_abort"}


def test_force_meanings_name_live_force_options(tree: Any) -> None:
    """Every FORCE_MEANINGS entry names a live command that has --force (no stale entry)."""
    with_force = {lf.name for lf in tree.leaves if "--force" in spec_check.opt_names(lf)}
    assert set(spec_check.FORCE_MEANINGS) == with_force


def test_short_flag_registry_is_pinned(tree: Any) -> None:
    """Every short flag in the tree is in SHORT_FLAGS (a new one needs an entry)."""
    found = spec_check.r_short_one_meaning(spec_check.Context(tree))
    assert not [s for s, why in found.items() if "not in SHORT_FLAGS" in why]


def _env_context(tree: Any, source: str, constants: dict[str, str]) -> Any:
    ctx = spec_check.Context(tree)
    ctx._cache["package_modules"] = [("commands/demo.py", ast.parse(source))]
    ctx._cache["env_constants"] = constants
    return ctx


def test_env_registry_complete(tree: Any) -> None:
    """EV1: an unregistered or computed read fails, and so does an entry nothing reads."""
    source = (
        "import os, typer\n"
        "ENV_DIR = 'KBAGENT_CONFIG_DIR'\n"
        "def cmd(name):\n"
        "    os.environ.get(ENV_DIR)\n"
        "    os.getenv('KBAGENT_NOT_REGISTERED')\n"
        "    os.environ.get(name)\n"
        "    _env_flag_set('SSH_TTY')\n"
        "    typer.Option(None, envvar='KBC_TOKEN')\n"
        "def _env_flag_set(name):\n"
        "    return bool(os.environ.get(name))\n"
    )
    found = spec_check.r_env_registry_complete(
        _env_context(tree, source, {"ENV_DIR": "KBAGENT_CONFIG_DIR"})
    )
    assert "KBAGENT_NOT_REGISTERED" in found
    assert "commands/demo.py:cmd os.environ.get" in found
    # A read helper's own body is skipped: its callers name the variable.
    assert not any(s.startswith("commands/demo.py:_env_flag_set") for s in found)
    for name in ("KBAGENT_CONFIG_DIR", "SSH_TTY", "KBC_TOKEN"):
        assert name not in found and f"{name} (registered)" not in found
    assert "KBAGENT_PROJECT (registered)" in found
    # A variable that a dependency reads needs no reader in src/.
    assert "NO_COLOR (registered)" not in found
    # Nothing reads the family here, so its entry is reported.
    assert "KBC_MASTER_TOKEN_<ALIAS> (registered)" in found


def test_env_registry_reads_subscripts_membership_and_families(tree: Any) -> None:
    source = (
        "import os\n"
        "def cmd(alias):\n"
        "    os.environ['KBAGENT_PROJECT']\n"
        "    os.environ['KBAGENT_SUBSCRIPT_UNKNOWN']\n"
        "    os.environ['KBAGENT_WRITTEN'] = '1'\n"
        "    if 'KBAGENT_IN_UNKNOWN' in os.environ: pass\n"
        "    if 'KBAGENT_SKIP_UPDATE' not in os.environ: pass\n"
        "    key = f'{ENV_MASTER}_{alias.upper()}'\n"
        "    os.environ.get(key)\n"
        "    os.environ.get(f'{ENV_OTHER}_{alias}')\n"
    )
    constants = {"ENV_MASTER": "KBC_MASTER_TOKEN", "ENV_OTHER": "KBAGENT_UNKNOWN_FAMILY"}
    found = spec_check.r_env_registry_complete(_env_context(tree, source, constants))
    assert "KBAGENT_SUBSCRIPT_UNKNOWN" in found
    assert "KBAGENT_IN_UNKNOWN" in found
    assert "KBAGENT_WRITTEN" not in found  # an assignment is not a read
    for name in ("KBAGENT_PROJECT", "KBAGENT_SKIP_UPDATE", "KBC_MASTER_TOKEN_<ALIAS>"):
        assert name not in found and f"{name} (registered)" not in found
    assert "KBAGENT_UNKNOWN_FAMILY_<" in found
    assert not any("os.environ.get" in s for s in found)


def test_fanout_reads_are_the_reads_with_all_projects(tree: Any) -> None:
    """FANOUT_READS lists every read command that has --all-projects (T2), and no other."""
    reads = {
        lf.name
        for lf in tree.leaves
        if lf.category == "read" and "--all-projects" in spec_check.opt_names(lf)
    }
    # `sync diff` and `sync pull` have their own --all-projects (one tree per project).
    assert reads - {"sync diff", "sync pull"} == spec_check.FANOUT_READS
