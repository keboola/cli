#!/usr/bin/env python3
"""CI canary: diff the live keboola-mcp-server tool catalog against the parity map.

Fetches ``TOOLS.md`` from the keboola/mcp-server default branch, extracts the
tool index (``- [tool_name](#tool_name): ...`` lines), and compares it with
``keboola_agent_cli.mcp_parity.MCP_TOOL_PARITY``:

- a catalog tool MISSING from the map -> exit 1 (a new server tool shipped;
  port it or map it to an existing command BEFORE the passthrough removal
  widens the gap -- epic #390),
- a mapped tool gone from the catalog -> warning only (upstream removed it;
  prune the entry at leisure).

Runs from the weekly ``mcp-parity-canary`` workflow and ``make parity-check``.
Network-dependent by design -- deliberately NOT part of ``make check`` / the
PR-blocking test job. Offline invariants live in tests/test_mcp_parity_map.py.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import urllib.request
from pathlib import Path


def _load_parity_map() -> dict:
    """Load MCP_TOOL_PARITY straight from the module file.

    Bypasses ``keboola_agent_cli/__init__.py`` (which imports the SDK facade
    and its third-party deps) so the canary runs on a bare python3 in CI --
    ``mcp_parity.py`` itself is stdlib-only.
    """
    module_path = Path(__file__).parent.parent / "src" / "keboola_agent_cli" / "mcp_parity.py"
    spec = importlib.util.spec_from_file_location("mcp_parity", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves cls.__module__ through sys.modules at class-creation
    # time -- exec without registration crashes on python >= 3.13.
    sys.modules["mcp_parity"] = module
    spec.loader.exec_module(module)
    return module.MCP_TOOL_PARITY


MCP_TOOL_PARITY = _load_parity_map()

TOOLS_MD_URL = "https://raw.githubusercontent.com/keboola/mcp-server/main/TOOLS.md"
_INDEX_LINE = re.compile(r"^- \[(?P<name>[a-z0-9_]+)\]\(#(?P=name)\):", re.MULTILINE)


def fetch_catalog() -> set[str]:
    with urllib.request.urlopen(TOOLS_MD_URL, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    names = set(_INDEX_LINE.findall(body))
    if not names:
        raise RuntimeError(
            "No tool index lines matched in TOOLS.md -- the upstream format "
            "changed; fix _INDEX_LINE in scripts/check_mcp_parity.py."
        )
    return names


def main() -> int:
    catalog = fetch_catalog()
    mapped = set(MCP_TOOL_PARITY)

    unmapped = sorted(catalog - mapped)
    stale = sorted(mapped - catalog)

    print(f"catalog: {len(catalog)} tools, parity map: {len(mapped)} entries")
    if stale:
        print(
            "\nWARNING: mapped tools no longer in the upstream catalog "
            "(prune from mcp_parity.py when convenient):"
        )
        for name in stale:
            print(f"  - {name}")
    if unmapped:
        print(
            "\nFAIL: upstream tools with NO parity-map entry -- decide the "
            "native equivalent (or port) and add it to "
            "src/keboola_agent_cli/mcp_parity.py (epic #390):"
        )
        for name in unmapped:
            print(f"  - {name}")
        return 1

    print("OK: every upstream tool has a parity-map entry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
