"""Tests for scripts/check_file_size.py -- the code-line budget gate.

Two things must hold for the gate to be worth having:

1. The **measurement** is honest. Raw LOC was rejected precisely because it
   taxes docstrings, so the code-line count has to exclude prose exactly and
   not quietly exclude real code (a module-level data string is code).
2. The **ratchet** actually ratchets. A grandfathered file may shrink but never
   grow, and a brand-new oversized file is rejected outright.

The real repo tree is never mutated: measurement runs against tmp files and the
ratchet logic against synthetic metrics.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load scripts/check_file_size.py as a module without having to install it.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "_check_file_size_under_test",
    SCRIPTS_DIR / "check_file_size.py",
)
assert SPEC is not None and SPEC.loader is not None
_mod = importlib.util.module_from_spec(SPEC)
sys.modules["_check_file_size_under_test"] = _mod
SPEC.loader.exec_module(_mod)


def _measure_source(tmp_path: Path, source: str):
    target = tmp_path / "sample.py"
    target.write_text(source, encoding="utf-8")
    return _mod.measure(target)


class TestMeasurement:
    """What counts as a code line."""

    def test_docstrings_and_comments_are_not_code(self, tmp_path):
        metrics = _measure_source(
            tmp_path,
            '''"""Module docstring.

Spanning several lines.
"""

# A standalone comment.
def f() -> int:
    """Function docstring."""
    return 1
''',
        )
        # Code lines: `def f() -> int:` and `return 1`.
        assert metrics.code == 2
        assert metrics.docstring == 5  # 4-line module docstring + 1-line function one
        assert metrics.comment == 1
        assert metrics.blank == 2

    def test_module_level_data_string_counts_as_code(self, tmp_path):
        """A string ASSIGNED to a name is data, not prose.

        This is the line between `changelog.py` (data, counted) and a docstring
        (prose, exempt); getting it wrong would let real content hide from the
        budget behind a triple quote.
        """
        metrics = _measure_source(
            tmp_path,
            'TEMPLATE = """\nline one\nline two\nline three\n"""\n',
        )
        assert metrics.code == 5
        assert metrics.docstring == 0

    def test_class_and_nested_function_docstrings_are_found(self, tmp_path):
        metrics = _measure_source(
            tmp_path,
            '''class A:
    """Class docstring."""

    def m(self) -> None:
        """Method docstring."""
        pass
''',
        )
        assert metrics.docstring == 2
        assert metrics.code == 3  # class A:, def m, pass

    def test_multiline_expression_counts_each_line_once(self, tmp_path):
        metrics = _measure_source(tmp_path, "x = [\n    1,\n    2,\n]\n")
        assert metrics.code == 4

    def test_trailing_comment_does_not_shadow_its_code_line(self, tmp_path):
        """A comment riding on a code line must not be double-counted."""
        metrics = _measure_source(tmp_path, "x = 1  # explain\n")
        assert metrics.code == 1
        assert metrics.comment == 0

    def test_prose_ratio_reflects_documentation_weight(self, tmp_path):
        metrics = _measure_source(
            tmp_path,
            '"""Doc."""\n# comment\nx = 1\ny = 2\n',
        )
        assert metrics.total == 4
        assert metrics.prose_ratio == pytest.approx(0.5)


class TestBudgetResolution:
    """First matching prefix wins; everything else gets the default."""

    @pytest.mark.parametrize(
        ("relative_path", "expected_label"),
        [
            ("commands/storage.py", "commands"),
            ("services/version_service.py", "services"),
            ("client/queue.py", "client"),
            ("manage_client.py", "client"),
            ("server/app.py", "server"),
            ("sync/engine.py", "sync"),
            ("frozen_dist.py", "module"),
            ("auto_update.py", "module"),
        ],
    )
    def test_layer_is_resolved_from_the_path(self, relative_path, expected_label):
        assert _mod.budget_for(relative_path).label == expected_label

    def test_soft_is_always_below_hard(self):
        budgets = [budget for _, budget in _mod._BUDGETS] + [_mod._DEFAULT_BUDGET]
        assert all(b.soft < b.hard for b in budgets)


class TestRatchet:
    """Grandfathered files may shrink, never grow."""

    @pytest.fixture
    def baseline_file(self, tmp_path, monkeypatch):
        path = tmp_path / "baseline.json"
        monkeypatch.setattr(_mod, "BASELINE_PATH", path)
        return path

    def test_missing_baseline_is_an_empty_ratchet(self, baseline_file):
        assert _mod._load_baseline() == {}

    def test_roundtrip_records_only_over_ceiling_files(self, baseline_file, tmp_path):
        pkg = tmp_path / "pkg"
        (pkg / "services").mkdir(parents=True)
        monkeypatch_root = pkg
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "PKG_ROOT", monkeypatch_root)
            big = _mod.FileMetrics(
                path=pkg / "services" / "huge.py",
                total=3000,
                code=1600,  # services hard ceiling is 1500
                docstring=0,
                comment=0,
                blank=0,
            )
            small = _mod.FileMetrics(
                path=pkg / "services" / "fine.py",
                total=100,
                code=50,
                docstring=0,
                comment=0,
                blank=0,
            )
            recorded = _mod._write_baseline([big, small])
        assert recorded == {"services/huge.py": 1600}
        assert _mod._load_baseline() == {"services/huge.py": 1600}

    def test_baseline_payload_explains_itself(self, baseline_file, tmp_path):
        """The file is read by humans mid-review; it must say what it is."""
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(_mod, "PKG_ROOT", tmp_path)
            _mod._write_baseline([])
        payload = json.loads(baseline_file.read_text(encoding="utf-8"))
        assert "may only shrink" in payload["_comment"]
        assert payload["files"] == {}


class TestRepoState:
    """The gate must be green on the tree it ships with.

    A blocking check that is red on arrival gets disabled, not fixed -- which is
    exactly why the ratchet exists. If this fails, either a file grew past its
    grandfathered size or new oversized code landed.
    """

    def test_repo_passes_its_own_gate(self, capsys):
        assert _mod.main([]) == 0

    def test_every_baselined_file_still_exists(self):
        """A stale baseline entry silently grants a budget to nothing."""
        for relative_path in _mod._load_baseline():
            assert (_mod.PKG_ROOT / relative_path).is_file(), (
                f"{relative_path} is baselined but gone -- run `make loc-baseline`"
            )

    def test_exemptions_are_justified_and_real(self):
        for relative_path, reason in _mod._EXEMPT.items():
            assert (_mod.PKG_ROOT / relative_path).is_file() or (
                relative_path in {p.name for p in _mod.PKG_ROOT.rglob("*.py")}
            ), f"{relative_path} is exempt but does not exist"
            assert len(reason) > 40, f"{relative_path} needs a real justification"
