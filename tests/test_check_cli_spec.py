"""Tests for scripts/check_cli_spec.py -- the `make cli-spec-check` gate.

They pin four things:

  1. The committed repository passes: the baseline and docs/cli-manifest.json
     match the live tree.
  2. The walk of the check sees the same commands as
     scripts/check_command_sync.py (proposal section 7.4), so the two cannot
     drift apart.
  3. The ratchet fails on a new violation, a stale baseline entry, an unknown
     rule, growth against the base branch and a loosened blocking flag; a
     non-blocking rule never fails.
  4. Single rules find what they are for on synthetic input.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
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
    assert fails == [f"FAIL grown    {RULE}: branch delete -- the baseline gained an entry"]


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
    manifest.write_text(spec_check.manifest_text(tree).replace("Scheduled agent", "Agent"))
    monkeypatch.setattr(spec_check, "MANIFEST_PATH", manifest)
    assert spec_check.r_manifest_fresh(spec_check.Context(tree)) == {
        "docs/cli-manifest.json": "differs from the live tree: agent; run 'make cli-manifest'"
    }


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
    assert set(found) == {"commands/demo.py:cmd"}


def test_short_flag_registry_is_pinned(tree: Any) -> None:
    """Every short flag in the tree is in SHORT_FLAGS (a new one needs an entry)."""
    found = spec_check.r_short_one_meaning(spec_check.Context(tree))
    assert not [s for s, why in found.items() if "not in SHORT_FLAGS" in why]
