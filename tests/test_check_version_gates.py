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
residue = check_version_gates.find_vnext_residue


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


class TestVnextResidue:
    """``vNEXT`` is legal in a feature PR and fatal in a release PR.

    The separator is backticks: a placeholder quoted as inline code is prose
    *about* the mechanism, which is why the old release-checklist grep could
    never come back empty.
    """

    def test_bare_placeholder_is_a_gate(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "g.md", "- `--flag` (since vNEXT) does a thing\n")
        found = residue([f])
        assert len(found) == 1
        assert found[0].line == 1

    def test_plus_form_is_a_gate(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "g.md", "intro\n- **vNEXT+**: resolves to the first project\n")
        assert len(residue([f])) == 1

    def test_inline_code_mention_is_prose(self, tmp_path: Path) -> None:
        """The exact shapes CLAUDE.md / CONTRIBUTING.md use to teach the rule."""
        f = _write(
            tmp_path,
            "process.md",
            "tag it with the literal placeholder **`vNEXT`** -- `(since vNEXT)` / `vNEXT+`.\n"
            "the release PR replaces every `vNEXT` placeholder with the real version\n",
        )
        assert residue([f]) == []

    def test_gate_and_prose_on_the_same_line_still_flags(self, tmp_path: Path) -> None:
        """One quoted mention must not launder a live gate sharing the line."""
        f = _write(tmp_path, "mixed.md", "`vNEXT` is the placeholder; (since vNEXT) is live\n")
        assert len(residue([f])) == 1

    def test_numeric_gate_is_not_residue(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "n.md", "- `--flag` (since v0.90.0)\n- other 0.73.0+\n")
        assert residue([f]) == []

    def test_backticks_do_not_hide_numeric_gates(self, tmp_path: Path) -> None:
        """The asymmetry is deliberate: docs/sdk.md writes real gates as `0.66.0+`."""
        f = _write(tmp_path, "sdk.md", "### Device-enrollment primitives (`0.66.0+`)\n")
        assert list(collect([f])) == ["0.66.0"]

    def test_reports_path_line_and_text(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "d.md", "x\ny\n### What's-new popup *(since vNEXT)*\n")
        gate = residue([f])[0]
        assert gate.line == 3
        assert "What's-new popup" in gate.text


class TestLiveRepositoryVnext:
    """The live tree is scanned -- but a placeholder found in it is NOT a failure.

    Between releases ``main`` legitimately carries `(since vNEXT)` markers. The
    #648 process has every feature PR write the placeholder and ONLY the release
    PR rewrite them; CLAUDE.md states it outright ("Writing `vNEXT` in a feature
    PR is correct and stays green"), and TestReleaseModeSelection below spells
    out why arming outside a release PR "would demand a contributor delete a
    placeholder the process requires them to write".

    An unconditional "the live tree has no placeholders" assertion contradicts
    exactly that. It is green only in the window right after a release, and goes
    red for main AND for every open PR the moment the first feature PR of the
    next cycle lands. That is not hypothetical: it was added in #670 days after
    0.90.0 shipped -- tree empty, so it passed -- and detonated on #675, taking
    main and every in-flight PR with it.

    The release-time requirement is real, but deciding whether the gate arms
    needs the BASE branch's version, which a unit test cannot see. That check
    already exists where it can: the "Unresolved vNEXT placeholder check" step
    in .github/workflows/ci.yml passes `--release-if-newer-than`, and
    `make vnext-check` is its local twin. What is left for a test is that the
    live scan actually WORKS -- which is what the two below assert.
    """

    def test_live_scan_reports_well_formed_gates(self) -> None:
        """Whatever the tree currently carries, every hit must be real.

        This is the half that has a stable answer: the globs resolve, the files
        are readable, and each reported gate points at a line that genuinely
        contains the placeholder. A hit count of zero is just as valid as ten --
        it depends on where in the release cycle the tree happens to be.
        """
        for gate in residue(check_version_gates.resolve_paths()):
            # `gate.path` is REPO-ROOT-RELATIVE (find_vnext_residue stores
            # `path.relative_to(REPO_ROOT)`), so it must be re-anchored rather
            # than resolved against the cwd -- otherwise this only works when
            # pytest happens to be invoked from the repo root. Its one fallback
            # branch stores an absolute path instead; `REPO_ROOT / <absolute>`
            # yields that path unchanged, so both cases are covered.
            lines = (
                (check_version_gates.REPO_ROOT / gate.path).read_text(errors="replace").splitlines()
            )
            assert 1 <= gate.line <= len(lines), f"{gate.path}:{gate.line} is out of range"
            assert check_version_gates.VNEXT_TOKEN in lines[gate.line - 1], (
                f"{gate.path}:{gate.line} was reported but does not carry the placeholder"
            )

    def test_prose_mentions_are_still_present_and_ignored(self) -> None:
        """Guards the guard: a rule that matched nothing would pass vacuously."""
        paths = check_version_gates.resolve_paths()
        mentions = sum(
            line.count(check_version_gates.VNEXT_TOKEN)
            for path in paths
            for line in path.read_text(errors="replace").splitlines()
        )
        assert mentions > 0, "no vNEXT mentions at all -- the scan globs are probably broken"


class TestReleaseModeSelection:
    """Only a version-RAISING branch is a release PR.

    CI hands the base branch's version to ``--release-if-newer-than``. A
    two-dot diff also fires for a stale feature branch whose base has since
    been released; arming there would demand a contributor delete a
    placeholder the process requires them to write.
    """

    def test_explicit_flag_forces_release_mode(self) -> None:
        assert check_version_gates.is_release_mode(["--release"]) is True

    def test_no_flag_is_not_release_mode(self) -> None:
        assert check_version_gates.is_release_mode([]) is False

    def test_higher_than_base_arms(self) -> None:
        """The release PR: pyproject moved above the base branch."""
        base = check_version_gates._pyproject_version()
        lower = f"0.0.{int(base.split('.')[-1].rstrip('abrc0123456789') or 0)}"
        assert check_version_gates.is_release_mode(["--release-if-newer-than", lower]) is True

    def test_equal_to_base_does_not_arm(self) -> None:
        """The ordinary feature PR -- it must stay free to write vNEXT."""
        base = check_version_gates._pyproject_version()
        assert check_version_gates.is_release_mode(["--release-if-newer-than", base]) is False

    def test_lower_than_base_does_not_arm(self) -> None:
        """A stale branch behind a released main -- the false positive to avoid."""
        assert check_version_gates.is_release_mode(["--release-if-newer-than", "99.0.0"]) is False

    def test_leading_v_is_tolerated(self) -> None:
        base = check_version_gates._pyproject_version()
        assert check_version_gates.is_release_mode(["--release-if-newer-than", f"v{base}"]) is False

    def test_empty_base_does_not_arm(self) -> None:
        """An unreadable base must neither disarm silently nor fail a normal PR."""
        assert check_version_gates.is_release_mode(["--release-if-newer-than", ""]) is False

    def test_missing_argument_is_an_error(self) -> None:
        import pytest

        with pytest.raises(SystemExit):
            check_version_gates.is_release_mode(["--release-if-newer-than"])


class TestVnextFencedBlocks:
    """Fenced blocks are deliberately NOT exempt -- this pins the trade-off.

    ``CLAUDE.md``'s ``## All CLI Commands`` section is one giant fence carrying
    real gates: exempting fences dropped 2 of 16 live gates in the pre-0.90.0
    tree, silently. A doc wanting to SHOW the placeholder uses inline backticks.
    """

    def test_gate_inside_a_fence_is_still_a_gate(self) -> None:
        """The CLAUDE.md shape: a command list fenced whole, with gates inside."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "commands.md"
            f.write_text("```\n# component detail (since vNEXT): falls back to ...\n```\n")
            found = residue([f])
        assert len(found) == 1, "a fenced gate must not be exempt -- see the class docstring"

    def test_inline_backticks_remain_the_escape_hatch(self, tmp_path: Path) -> None:
        f = _write(tmp_path, "doc.md", "Resolve them: run `grep -rn '(since vNEXT)' docs/`\n")
        assert residue([f]) == []


class TestPythonSourcesAreScanned:
    """``src/**/*.py`` is in scope -- its absence shipped a placeholder in 0.90.1.

    A gate in a Python comment is agent-facing documentation exactly like a
    markdown one, and until this glob existed only one hand-picked file under
    ``src/`` was read: the placeholder in ``permissions.py`` went out in a
    release, and a stale ``v0.26.1`` marker in ``commands/project.py`` -- a
    version that never shipped -- sat undetected for three months.
    """

    def test_scanned_globs_cover_python_sources(self) -> None:
        """Cheap guard: the glob cannot be dropped again without a red test."""
        assert any(
            pattern.startswith("src/") and pattern.endswith("*.py")
            for pattern in check_version_gates.SCANNED_GLOBS
        ), f"src Python sources must stay in scope: {check_version_gates.SCANNED_GLOBS}"

    def test_the_scan_reaches_a_file_that_used_to_be_invisible(self) -> None:
        """Behavioural half: a glob can be present and still resolve to nothing.

        ``permissions.py`` is the file whose placeholder shipped in 0.90.1 and
        was never covered by the old hand-picked entry, so it is the honest
        witness that the widened scan actually reads more than it used to.
        """
        scanned = {p.name for p in check_version_gates.resolve_paths()}
        assert "permissions.py" in scanned
        assert "project.py" in scanned

    def test_scripts_are_not_scanned(self) -> None:
        """The gate script names the placeholder it hunts -- scanning it self-flags."""
        assert not any(
            pattern.startswith("scripts/") for pattern in check_version_gates.SCANNED_GLOBS
        ), "scripts/ cannot be scanned: check_version_gates.py itself quotes vNEXT"

    def test_bare_placeholder_in_a_python_comment_is_a_gate(self, tmp_path: Path) -> None:
        """The exact 0.90.1 shape: a registry comment tagged with the placeholder."""
        f = _write(
            tmp_path,
            "registry.py",
            '"""Module docstring."""\n\n# Serve-only (since vNEXT): a new REST operation.\nX = 1\n',
        )
        found = residue([f])
        assert len(found) == 1
        assert found[0].line == 3

    def test_python_files_contribute_numeric_gates(self, tmp_path: Path) -> None:
        """A `(since vX.Y.Z)` in a comment is audited against CHANGELOG like any other."""
        f = _write(tmp_path, "cmd.py", "# ── Project members (since v0.29.0) ──\n")
        assert list(collect([f])) == ["0.29.0"]


class TestDoubleBacktickSpans:
    """RST-style ``x`` spans count as inline code -- the docstring style under ``src/``.

    Without this the first ``(since vNEXT)`` written in a Python docstring
    becomes a false positive; with it, markdown behaviour is unchanged (a
    single-backtick span is still a span, and a bare placeholder is still live).
    """

    def test_double_backtick_span_is_prose(self, tmp_path: Path) -> None:
        f = _write(
            tmp_path, "mod.py", '"""Tag new behavior ``(since vNEXT)`` in a feature PR."""\n'
        )
        assert residue([f]) == []

    def test_single_backtick_span_is_still_prose(self, tmp_path: Path) -> None:
        """Regression: widening the pattern must not break the markdown case."""
        f = _write(tmp_path, "doc.md", "the literal `vNEXT` placeholder\n")
        assert residue([f]) == []

    def test_bare_placeholder_beside_a_double_backtick_span_still_flags(
        self, tmp_path: Path
    ) -> None:
        """One quoted mention must not launder a live gate sharing the line."""
        f = _write(tmp_path, "mod.py", '"""``vNEXT`` is the token; (since vNEXT) is live."""\n')
        assert len(residue([f])) == 1

    def test_double_backticks_do_not_hide_numeric_gates(self, tmp_path: Path) -> None:
        """The GATE_RE asymmetry survives: code spans are never stripped for versions."""
        f = _write(tmp_path, "mod.py", '"""Device-enrollment primitives (``0.66.0+``)."""\n')
        assert list(collect([f])) == ["0.66.0"]
