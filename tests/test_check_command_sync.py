"""Tests for scripts/check_command_sync.py -- the silent-drift CI gate.

The gate makes the live Typer command tree the single source of truth and
fails when a mirror surface (OPERATION_REGISTRY, CLAUDE.md, AGENT_CONTEXT,
commands-reference.md) drifts from it. These tests pin two things:

  1. The committed repo is CLEAN -- otherwise the gate would block every PR.
  2. `find_drift` actually detects each drift class on synthetic inputs, and
     honours the per-surface granularity (full-leaf vs 2-segment).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Load scripts/check_command_sync.py as a module without installing it
# (mirrors tests/test_sync_version_script.py).
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "_check_command_sync_under_test",
    SCRIPTS_DIR / "check_command_sync.py",
)
assert SPEC is not None and SPEC.loader is not None
_mod = importlib.util.module_from_spec(SPEC)
sys.modules["_check_command_sync_under_test"] = _mod
SPEC.loader.exec_module(_mod)


# --------------------------------------------------------------------------
# Live-tree integration -- the current repo must pass
# --------------------------------------------------------------------------


def test_live_command_tree_has_no_drift(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed repo must be clean; a red `main()` would block every PR."""
    monkeypatch.setattr(sys, "argv", ["check_command_sync.py"])
    assert _mod.main() == 0
    assert "OK:" in capsys.readouterr().out


def test_collect_commands_excludes_hidden_alias() -> None:
    leaves, groups = _mod.collect_commands()
    assert leaves, "expected a non-empty command tree"
    # `sl` is a hidden alias for `semantic-layer` -- it must never surface.
    assert not any(p and p[0] == "sl" for p in leaves)
    assert not any(p and p[0] == "sl" for p in groups)
    # ...but the real (visible) group it aliases is present.
    assert any(p[0] == "semantic-layer" for p in leaves)


def test_list_flag_prints_tree(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", ["check_command_sync.py", "--list"])
    assert _mod.main() == 0
    assert "config list" in capsys.readouterr().out


# --------------------------------------------------------------------------
# find_drift unit tests -- synthetic inputs
# --------------------------------------------------------------------------


def _clean_surfaces() -> dict[str, object]:
    """A minimal, fully-consistent surface set for the leaf ('config','list')."""
    return {
        "registry_keys": {"config", "config.list"},
        "claude_text": "kbagent config list --project NAME",
        "context_text": "config list",
        "reference_text": "config list",
    }


def test_find_drift_clean_synthetic() -> None:
    assert _mod.find_drift([("config", "list")], [("config",)], **_clean_surfaces()) == []


def test_find_drift_detects_missing_registry_entry() -> None:
    surfaces = _clean_surfaces()
    surfaces["registry_keys"] = {"config"}  # leaf 'config.list' is not categorised
    problems = _mod.find_drift([("config", "list")], [("config",)], **surfaces)
    blob = "\n".join(problems)
    assert "OPERATION_REGISTRY" in blob
    assert "config.list" in blob


def test_find_drift_detects_dead_registry_entry() -> None:
    surfaces = _clean_surfaces()
    surfaces["registry_keys"] = {"config", "config.list", "ghost.removed"}
    problems = _mod.find_drift([("config", "list")], [("config",)], **surfaces)
    blob = "\n".join(problems)
    assert "ghost.removed" in blob
    assert "no live command" in blob


def test_find_drift_detects_missing_documentation() -> None:
    surfaces = _clean_surfaces()
    surfaces["claude_text"] = ""
    surfaces["context_text"] = ""
    surfaces["reference_text"] = ""
    problems = _mod.find_drift([("config", "list")], [("config",)], **surfaces)
    blob = "\n".join(problems)
    assert "CLAUDE.md" in blob
    assert "AGENT_CONTEXT" in blob
    assert "commands-reference.md" in blob


def test_serve_only_key_is_exempt_from_the_dead_key_check() -> None:
    surfaces = _clean_surfaces()
    surfaces["registry_keys"] = {"config", "config.list", "auth.projects"}
    problems = _mod.find_drift(
        [("config", "list")],
        [("config",)],
        serve_only_keys=frozenset({"auth.projects"}),
        **surfaces,
    )
    assert problems == []


def test_serve_only_key_still_counts_as_categorised_once_a_cli_leaf_exists() -> None:
    """The exemption must not leak into the MISSING-registry check.

    If `auth projects` ever becomes a real CLI leaf, its registry key is
    already there -- reporting it as missing would send the author to add a
    duplicate entry.
    """
    surfaces = _clean_surfaces()
    surfaces["registry_keys"] = {"config", "config.list", "auth", "auth.projects"}
    surfaces["claude_text"] = "kbagent config list --project NAME\nkbagent auth projects"
    surfaces["context_text"] = "config list\nauth projects"
    surfaces["reference_text"] = "config list\nauth projects"
    problems = _mod.find_drift(
        [("config", "list"), ("auth", "projects")],
        [("config",), ("auth",)],
        serve_only_keys=frozenset({"auth.projects"}),
        **surfaces,
    )
    assert problems == []


def test_claude_is_full_leaf_while_reference_is_two_segment() -> None:
    """A 3-level leaf: CLAUDE.md needs the full path; context/reference the 2-seg prefix."""
    leaves = [("grp", "add", "metric")]
    groups = [("grp",), ("grp", "add")]
    problems = _mod.find_drift(
        leaves,
        groups,
        registry_keys={"grp", "grp.add", "grp.add.metric"},
        # Docs mention only the compact 2-segment form "grp add"...
        claude_text="grp add",
        context_text="grp add",
        reference_text="grp add",
    )
    blob = "\n".join(problems)
    # ...so CLAUDE.md (full-leaf) complains about the missing "grp add metric"...
    assert "CLAUDE.md" in blob
    assert "grp add metric" in blob
    # ...but the 2-segment surfaces are satisfied by "grp add".
    assert "AGENT_CONTEXT" not in blob
    assert "commands-reference.md" not in blob
