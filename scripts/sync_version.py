#!/usr/bin/env python3
"""Sync version from pyproject.toml to plugin.json.

Ensures the plugin marketplace version matches the package version.
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


def main() -> None:
    version = get_pyproject_version()
    changed = sync_plugin_json(version)
    if changed:
        print(f"Updated plugin.json to {version}")
    else:
        print(f"plugin.json already at {version}")


if __name__ == "__main__":
    main()
