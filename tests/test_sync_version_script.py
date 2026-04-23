"""Regression tests for scripts/sync_version.py.

The sync script is the single source of truth for keeping three files in
lock-step: pyproject.toml (authoritative), plugins/kbagent/.claude-plugin/
plugin.json, and .claude-plugin/marketplace.json plugins[name=kbagent].version.

These tests use monkeypatch on the module-level paths so the real repo
files are never touched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

# Load scripts/sync_version.py as a module without having to install it.
SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
SPEC = importlib.util.spec_from_file_location(
    "_sync_version_under_test",
    SCRIPTS_DIR / "sync_version.py",
)
assert SPEC is not None and SPEC.loader is not None
_sync = importlib.util.module_from_spec(SPEC)
sys.modules["_sync_version_under_test"] = _sync
SPEC.loader.exec_module(_sync)


@pytest.fixture
def isolated_repo(tmp_path: Path, monkeypatch):
    """Lay out a miniature repo tree the sync script can operate on."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "keboola-agent-cli"\nversion = "9.9.9"\n',
        encoding="utf-8",
    )

    plugin_json = tmp_path / "plugins" / "kbagent" / ".claude-plugin" / "plugin.json"
    plugin_json.parent.mkdir(parents=True)
    plugin_json.write_text(
        json.dumps({"name": "kbagent", "version": "0.0.0"}, indent=2) + "\n",
        encoding="utf-8",
    )

    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    marketplace_json.parent.mkdir(parents=True)
    marketplace_json.write_text(
        json.dumps(
            {
                "name": "keboola-agent-cli",
                "version": "1.0.0",
                "plugins": [
                    {
                        "name": "kbagent",
                        "source": "./plugins/kbagent",
                        "description": "something",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(_sync, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(_sync, "PYPROJECT", pyproject)
    monkeypatch.setattr(_sync, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(_sync, "MARKETPLACE_JSON", marketplace_json)

    return {
        "root": tmp_path,
        "pyproject": pyproject,
        "plugin_json": plugin_json,
        "marketplace_json": marketplace_json,
    }


def test_get_pyproject_version_reads_source(isolated_repo: dict) -> None:
    assert _sync.get_pyproject_version() == "9.9.9"


def test_sync_plugin_json_updates_version(isolated_repo: dict) -> None:
    changed = _sync.sync_plugin_json("9.9.9")
    assert changed is True
    data = json.loads(isolated_repo["plugin_json"].read_text(encoding="utf-8"))
    assert data["version"] == "9.9.9"


def test_sync_plugin_json_idempotent(isolated_repo: dict) -> None:
    _sync.sync_plugin_json("9.9.9")
    assert _sync.sync_plugin_json("9.9.9") is False


def test_sync_marketplace_json_adds_plugin_version(isolated_repo: dict) -> None:
    """The kbagent entry starts without a version -- we should add it."""
    changed = _sync.sync_marketplace_json("9.9.9")
    assert changed is True

    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    kb = next(p for p in data["plugins"] if p["name"] == "kbagent")
    assert kb["version"] == "9.9.9"


def test_sync_marketplace_json_places_version_right_after_name(
    isolated_repo: dict,
) -> None:
    """Layout check: name -> version -> ... -- easy to spot in reviews."""
    _sync.sync_marketplace_json("9.9.9")
    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    kb = next(p for p in data["plugins"] if p["name"] == "kbagent")
    keys = list(kb.keys())
    assert keys[0] == "name"
    assert keys[1] == "version"


def test_sync_marketplace_json_idempotent(isolated_repo: dict) -> None:
    _sync.sync_marketplace_json("9.9.9")
    assert _sync.sync_marketplace_json("9.9.9") is False


def test_sync_marketplace_json_does_not_touch_top_level_version(
    isolated_repo: dict,
) -> None:
    """Top-level `version` is the marketplace DESCRIPTOR version, not the
    plugin version; the sync script must leave it alone."""
    _sync.sync_marketplace_json("9.9.9")
    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    assert data["version"] == "1.0.0"  # unchanged


def test_sync_marketplace_json_updates_existing_version(
    isolated_repo: dict,
) -> None:
    """Entry that already has a version gets bumped, not duplicated."""
    # Seed with a prior version
    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    for p in data["plugins"]:
        if p["name"] == "kbagent":
            p["version"] = "0.1.0"
    isolated_repo["marketplace_json"].write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )

    changed = _sync.sync_marketplace_json("9.9.9")
    assert changed is True

    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    kb = next(p for p in data["plugins"] if p["name"] == "kbagent")
    assert kb["version"] == "9.9.9"


def test_sync_marketplace_json_missing_plugin_entry_is_noop(isolated_repo: dict, capsys) -> None:
    """If marketplace.json has no kbagent plugin, warn and do nothing
    destructive."""
    data = json.loads(isolated_repo["marketplace_json"].read_text(encoding="utf-8"))
    data["plugins"] = []
    isolated_repo["marketplace_json"].write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )

    changed = _sync.sync_marketplace_json("9.9.9")
    assert changed is False
    err = capsys.readouterr().err
    assert "no plugin entry" in err.lower()


def test_sync_marketplace_json_absent_file_is_skipped(isolated_repo: dict, monkeypatch) -> None:
    """Repos that don't have a marketplace.json at all (e.g. forks) should
    not crash -- just skip the marketplace step."""
    missing = isolated_repo["root"] / "does-not-exist.json"
    monkeypatch.setattr(_sync, "MARKETPLACE_JSON", missing)
    assert _sync.sync_marketplace_json("9.9.9") is False


def test_main_runs_end_to_end(isolated_repo: dict, capsys) -> None:
    _sync.main()
    out = capsys.readouterr().out
    assert "Updated plugin.json to 9.9.9" in out
    assert "Updated marketplace.json" in out

    # Second run prints the idempotent message on both targets.
    _sync.main()
    out = capsys.readouterr().out
    assert "plugin.json already at 9.9.9" in out
    assert "marketplace.json" in out and "already at 9.9.9" in out
