#!/usr/bin/env python3
"""Sync version across the three version-bearing files.

Single source of truth: ``pyproject.toml``. The plugin manifest and the
marketplace catalogue are kept in lock-step so Claude Code sees the
same version string everywhere.

Files kept in sync:
- ``pyproject.toml``                                        (source)
- ``plugins/kbagent/.claude-plugin/plugin.json``            (plugin manifest)
- ``.claude-plugin/marketplace.json`` -> ``plugins[*].version``
  (per-plugin entry the marketplace descriptor exposes to Claude Code)

The marketplace's own top-level ``version`` key is NOT touched here: it
is the version of the marketplace *descriptor shape*, bumped only when
the catalogue's structure changes (e.g. when plugins are added or
removed), not on every plugin release.

Safe to run repeatedly (idempotent).

Usage:
    python scripts/sync_version.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
PLUGIN_JSON = REPO_ROOT / "plugins" / "kbagent" / ".claude-plugin" / "plugin.json"
MARKETPLACE_JSON = REPO_ROOT / ".claude-plugin" / "marketplace.json"

# Plugin name inside marketplace.json whose version must track the CLI/plugin.
KBAGENT_PLUGIN_NAME = "kbagent"


def get_pyproject_version() -> str:
    """Extract version from pyproject.toml."""
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        print("ERROR: could not find version in pyproject.toml", file=sys.stderr)
        sys.exit(1)
    return match.group(1)


def sync_plugin_json(version: str) -> bool:
    """Update plugin.json version. Returns True if changed."""
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    if data.get("version") == version:
        return False
    data["version"] = version
    PLUGIN_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def sync_marketplace_json(version: str) -> bool:
    """Update ``plugins[name=kbagent].version`` in marketplace.json.

    Returns True if changed.
    """
    if not MARKETPLACE_JSON.is_file():
        return False

    data = json.loads(MARKETPLACE_JSON.read_text(encoding="utf-8"))
    plugins = data.get("plugins") or []

    kbagent_entry: dict | None = None
    for entry in plugins:
        if isinstance(entry, dict) and entry.get("name") == KBAGENT_PLUGIN_NAME:
            kbagent_entry = entry
            break

    if kbagent_entry is None:
        print(
            f"WARN: marketplace.json has no plugin entry named '{KBAGENT_PLUGIN_NAME}'; "
            "skipping marketplace sync.",
            file=sys.stderr,
        )
        return False

    if kbagent_entry.get("version") == version:
        return False

    # Rewrite the plugin entry with `version` placed right after `name` so
    # the field is easy to spot when reviewing the file. Skip any existing
    # "version" key during iteration -- we re-inject it at the right spot.
    ordered: dict = {}
    for key, value in kbagent_entry.items():
        if key == "version":
            continue
        ordered[key] = value
        if key == "name":
            ordered["version"] = version
    if "version" not in ordered:
        # `name` key was missing for some reason; just append.
        ordered["version"] = version
    kbagent_entry.clear()
    kbagent_entry.update(ordered)

    MARKETPLACE_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def main() -> None:
    version = get_pyproject_version()

    plugin_changed = sync_plugin_json(version)
    if plugin_changed:
        print(f"Updated plugin.json to {version}")
    else:
        print(f"plugin.json already at {version}")

    marketplace_changed = sync_marketplace_json(version)
    if marketplace_changed:
        print(f"Updated marketplace.json plugins[{KBAGENT_PLUGIN_NAME}].version to {version}")
    else:
        print(f"marketplace.json plugins[{KBAGENT_PLUGIN_NAME}].version already at {version}")


if __name__ == "__main__":
    main()
