"""Unit tests for the version-gate audit (``scripts/check_version_gates.py``).

The gate exists because ``plugins/kbagent/agents/keboola-expert.md`` turns
``(since v0.84.0)`` / ``0.73.0+`` markers into a hard refusal rule: the agent
compares the user's installed version against the marker and declines anything
newer. A marker naming a version that never ships therefore makes the agent
refuse a flag the user actually has -- and no other check in the repo notices.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_version_gates.py"
_spec = importlib.util.spec_from_file_location("check_version_gates", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_version_gates = importlib.util.module_from_spec(_spec)
sys.modules["check_version_gates"] = check_version_gates
_spec.loader.exec_module(check_version_gates)

collect = check_version_gates.collect_gates


def _write(tmp_path: Path, name: str, body: str) -> Path:
    """Write a fixture inside pytest's tmp_path -- never into the repo tree."""
    target = tmp_path / name
    target.write_text(body)
    return target


class TestGateRegex:
    """Both documented gate syntaxes match; version-looking prose does not."""

    def test_since_form(self) -> None:
        assert check_version_gates.GATE_RE.search("foo *(since v0.84.0)*")

    def test_since_form_without_v(self) -> None:
        assert check_version_gates.GATE_RE.search("foo (since 0.84.0)")

    def test_plus_form(self) -> None:
        assert check_version_gates.GATE_RE.search("`--stage` (0.88.0+) picks")

    def test_plus_form_with_v(self) -> None:
        assert check_version_gates.GATE_RE.search("v0.88.0+ only")

    def test_bare_upstream_version_is_not_a_gate(self) -> None:
        """`keboola-mcp-server v1.76.2` is provenance, not a kbagent gate."""
        assert not check_version_gates.GATE_RE.search("verified against v1.76.2")

    def test_plain_prose_version_is_not_a_gate(self) -> None:
        assert not check_version_gates.GATE_RE.search("removed in 0.85.0, see docs")

    def test_four_part_version_is_not_a_gate(self) -> None:
        assert not check_version_gates.GATE_RE.search("schema 0.1.2.3+ here")


class TestCollectGates:
    def test_reports_path_and_line(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "doc.md", "intro\n\n`--flag` (0.73.0+) does a thing\n")
        gates = collect([f])
        assert list(gates) == ["0.73.0"]
        assert gates["0.73.0"][0][1] == 3

    def test_groups_multiple_markers_per_version(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "many.md", "a (since v0.80.0)\nb 0.80.0+\nc (since v0.81.0)\n")
        gates = collect([f])
        assert sorted(gates) == ["0.80.0", "0.81.0"]
        assert len(gates["0.80.0"]) == 2

    def test_file_without_gates_yields_nothing(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "plain.md", "no markers here at all\n")
        assert collect([f]) == {}


class TestLiveRepository:
    """The repo itself must stay clean -- this is the regression the gate guards."""

    def test_every_gate_resolves_to_a_released_version(self) -> None:
        from keboola_agent_cli.changelog import CHANGELOG

        gates = collect(check_version_gates.resolve_paths())
        unknown = {v: locs for v, locs in gates.items() if v not in CHANGELOG}
        assert unknown == {}, (
            "version gate names a version with no changelog entry; "
            "if a release was renumbered, rewrite the markers: "
            f"{ {v: locs[:2] for v, locs in unknown.items()} }"
        )

    def test_the_scan_actually_finds_gates(self) -> None:
        """Guards the guard: a broken glob would make the check vacuously pass."""
        gates = collect(check_version_gates.resolve_paths())
        assert len(gates) > 50
