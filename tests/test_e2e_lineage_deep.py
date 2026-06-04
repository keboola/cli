"""End-to-end tests for `kbagent lineage build/show` against real sync'd data.

Environment-agnostic: discovers tables and edges dynamically from the
actual lineage graph. Works with any sync'd workspace.

Requires: E2E_LINEAGE_DIR env var pointing to a sync'd workspace.

Run:
    E2E_LINEAGE_DIR=/tmp/lineage-ro uv run pytest tests/test_e2e_lineage_deep.py -v -s
    E2E_LINEAGE_DIR=/tmp/apify     uv run pytest tests/test_e2e_lineage_deep.py -v -s
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore

# ---------------------------------------------------------------------------
# Environment & skip logic
# ---------------------------------------------------------------------------

ENV_LINEAGE_DIR = "E2E_LINEAGE_DIR"

LINEAGE_DIR = os.environ.get(ENV_LINEAGE_DIR, "")
HAS_LINEAGE_DIR = bool(LINEAGE_DIR) and Path(LINEAGE_DIR).is_dir()

skip_without_lineage_dir = pytest.mark.skipif(
    not HAS_LINEAGE_DIR,
    reason=f"Lineage E2E tests require {ENV_LINEAGE_DIR} env var pointing to sync'd workspace",
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_MAX_RESPONSE_LEN = 500


def _format_cmd(args: list[str]) -> str:
    return "kbagent " + " ".join(args)


def _summarize(output: str, max_len: int = _MAX_RESPONSE_LEN) -> str:
    try:
        data = json.loads(output)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        if len(pretty) > max_len:
            return pretty[:max_len] + f"\n  ... ({len(pretty)} chars total)"
        return pretty
    except (json.JSONDecodeError, TypeError):
        text = output.strip()
        return text[:max_len] + "..." if len(text) > max_len else text


def _invoke(config_dir: Path, args: list[str]) -> Any:
    """Invoke the CLI with a custom config store."""
    print(f"\n  {_CYAN}$ {_format_cmd(args)}{_RESET}")

    with patch("keboola_agent_cli.cli.ConfigStore") as mock_cls:
        mock_cls.return_value = ConfigStore(config_dir=config_dir)
        result = runner.invoke(app, args, catch_exceptions=True)

    status = (
        f"{_GREEN}OK{_RESET}" if result.exit_code == 0 else f"{_RED}EXIT {result.exit_code}{_RESET}"
    )
    print(f"  {_DIM}-> {status} ({len(result.output)} bytes){_RESET}")
    for line in _summarize(result.output).split("\n")[:10]:
        print(f"  {_DIM}   {line}{_RESET}")

    return result


def _json_ok(result) -> dict[str, Any]:
    """Parse CLI output as JSON and assert status == ok."""
    assert result.exit_code == 0, (
        f"Command failed (exit {result.exit_code}):\n{result.output[:500]}"
    )
    data = json.loads(result.output)
    assert data.get("status") == "ok", f"Expected status=ok, got: {json.dumps(data)[:300]}"
    return data


def _step(num: int, title: str) -> None:
    print(f"\n{_BOLD}{'=' * 60}")
    print(f"  STEP {num}: {title}")
    print(f"{'=' * 60}{_RESET}")


def _find_table_with_downstream(cache_path: Path) -> str | None:
    """Find any table FQN that has at least one downstream edge."""
    with open(cache_path) as f:
        data = json.load(f)
    sources = {}
    for e in data["edges"]:
        if e["source_type"] == "table":
            sources[e["source_fqn"]] = sources.get(e["source_fqn"], 0) + 1
    if not sources:
        return None
    return max(sources, key=lambda k: sources.get(k, 0))


def _find_table_with_upstream(cache_path: Path) -> str | None:
    """Find any table FQN that has upstream edges (output of a config)."""
    with open(cache_path) as f:
        data = json.load(f)
    targets = {}
    for e in data["edges"]:
        if e["target_type"] == "table" and e["edge_type"] == "writes":
            targets[e["target_fqn"]] = targets.get(e["target_fqn"], 0) + 1
    if not targets:
        return None
    return max(targets, key=lambda k: targets.get(k, 0))


def _find_table_with_columns(cache_path: Path) -> tuple[str | None, str | None]:
    """Find a table FQN that has column data, and one column name."""
    with open(cache_path) as f:
        data = json.load(f)
    for e in data["edges"]:
        cols = e.get("columns", [])
        if cols and e["source_type"] == "table" and e["source_fqn"] in data.get("tables", {}):
            return e["source_fqn"], cols[0]
    # Fallback: any table with columns in metadata
    for fqn, t in data.get("tables", {}).items():
        if t.get("columns"):
            return fqn, t["columns"][0]
    return None, None


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------


@skip_without_lineage_dir
@pytest.mark.e2e
class TestE2ELineageDeep:
    """E2E tests for lineage build/show against real sync'd data."""

    @pytest.fixture(autouse=True)
    def setup(self) -> None:
        self.lineage_dir = Path(LINEAGE_DIR).resolve()
        self.config_dir = self.lineage_dir / ".kbagent"
        self.cache_file = self.lineage_dir / "lineage.json"

    # ── Build ──────────────────────────────────────────────────────

    def test_01_build_lineage(self) -> None:
        """Build lineage graph from sync'd data and save cache."""
        _step(1, "Build lineage from sync'd data")

        result = _invoke(
            self.config_dir,
            ["--json", "lineage", "build", "-d", str(self.lineage_dir), "-o", str(self.cache_file)],
        )
        data = _json_ok(result)

        summary = data["data"]["summary"]
        assert summary["tables"] > 0, "Expected tables in lineage"
        assert summary["configurations"] > 0, "Expected configs in lineage"
        assert summary["edges"] > 0, "Expected edges in lineage"

        methods = summary["detection_methods"]
        assert "input_mapping" in methods or "output_mapping" in methods, (
            "Expected at least input_mapping or output_mapping edges"
        )

        print(
            f"\n  {_GREEN}Summary: {summary['tables']} tables, "
            f"{summary['configurations']} configs, {summary['edges']} edges{_RESET}"
        )

    # ── Load from cache ────────────────────────────────────────────

    def test_02_info_summary(self) -> None:
        """lineage info shows graph contents (projects, top tables)."""
        _step(2, "Info summary")

        result = _invoke(self.config_dir, ["lineage", "info", "-l", str(self.cache_file)])
        assert result.exit_code == 0
        assert "Lineage Graph Contents" in result.output
        assert "Tables:" in result.output
        assert "Projects:" in result.output
        assert "Most connected tables" in result.output

    # ── Downstream query ───────────────────────────────────────────

    def test_03_downstream_json(self) -> None:
        """Query downstream in JSON mode with a dynamically discovered table."""
        _step(3, "Downstream query (JSON)")

        table = _find_table_with_downstream(self.cache_file)
        assert table, "No table with downstream edges found in lineage"

        result = _invoke(
            self.config_dir,
            [
                "--json",
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                table,
                "--depth",
                "2",
            ],
        )
        data = _json_ok(result)
        edges = data["data"]["edges"]
        assert len(edges) > 0, f"Expected downstream edges for {table}"
        print(f"\n  {_GREEN}Found {len(edges)} downstream edges for {table}{_RESET}")

    def test_04_downstream_human(self) -> None:
        """Query downstream in human mode."""
        _step(4, "Downstream query (human)")

        table = _find_table_with_downstream(self.cache_file)
        assert table, "No table with downstream edges found"

        result = _invoke(
            self.config_dir,
            ["lineage", "show", "-l", str(self.cache_file), "--downstream", table, "--depth", "1"],
        )
        assert result.exit_code == 0
        assert "Downstream dependents" in result.output

    # ── Upstream query ─────────────────────────────────────────────

    def test_05_upstream_json(self) -> None:
        """Query upstream in JSON mode with a dynamically discovered table."""
        _step(5, "Upstream query (JSON)")

        table = _find_table_with_upstream(self.cache_file)
        assert table, "No table with upstream edges found in lineage"

        result = _invoke(
            self.config_dir,
            ["--json", "lineage", "show", "-l", str(self.cache_file), "--upstream", table],
        )
        data = _json_ok(result)
        edges = data["data"]["edges"]
        assert len(edges) >= 1, f"Expected upstream edges for {table}"

        node_info = data["data"]["node_info"]
        assert node_info["type"] == "table"
        print(f"\n  {_GREEN}Found {len(edges)} upstream edges for {table}{_RESET}")

    # ── Column detail (--columns) ──────────────────────────────────

    def test_06_columns_flag(self) -> None:
        """--columns shows column names on edges."""
        _step(6, "Column detail (--columns flag)")

        table = _find_table_with_downstream(self.cache_file)
        assert table, "No table with downstream edges found"

        result = _invoke(
            self.config_dir,
            [
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                table,
                "--depth",
                "1",
                "--columns",
            ],
        )
        assert result.exit_code == 0
        # --columns should produce output (even if no column_mapping, it shows column lists)

    # ── Single column trace (-c) ───────────────────────────────────

    def test_07_column_trace(self) -> None:
        """Trace a specific column through lineage."""
        _step(7, "Column trace (-c)")

        table, col = _find_table_with_columns(self.cache_file)
        if not table or not col:
            pytest.skip("No table with column data found for column trace test")

        result = _invoke(
            self.config_dir,
            [
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                table,
                "--depth",
                "1",
                "--columns",
                "-c",
                col,
            ],
        )
        assert result.exit_code == 0
        assert f"column: {col}" in result.output
        print(f"\n  {_GREEN}Traced column '{col}' in {table}{_RESET}")

    # ── Node not found ─────────────────────────────────────────────

    def test_08_node_not_found_json(self) -> None:
        """Missing node returns structured error."""
        _step(8, "Node not found (JSON)")

        result = _invoke(
            self.config_dir,
            [
                "--json",
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--upstream",
                "nonexistent:fake.table",
            ],
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert "NODE_NOT_FOUND" in data["error"]["code"]

    def test_09_node_not_found_human(self) -> None:
        """Missing node shows error in human mode."""
        _step(9, "Node not found (human)")

        result = _invoke(
            self.config_dir,
            [
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                "nonexistent.table.xyz",
            ],
        )
        assert result.exit_code == 1

    def test_09b_show_without_query(self) -> None:
        """lineage show without --upstream/--downstream returns usage error."""
        _step(9, "Show without query -> error")

        result = _invoke(
            self.config_dir,
            ["lineage", "show", "-l", str(self.cache_file)],
        )
        assert result.exit_code == 2
        assert "lineage info" in result.output

    # ── Missing cache file ─────────────────────────────────────────

    def test_10_missing_cache(self) -> None:
        """Non-existent cache file returns error."""
        _step(10, "Missing cache file")

        result = _invoke(
            self.config_dir,
            ["--json", "lineage", "show", "-l", "/tmp/does_not_exist_lineage.json"],
        )
        assert result.exit_code == 1

    # ── Depth control ──────────────────────────────────────────────

    def test_12_depth_limits_traversal(self) -> None:
        """--depth 1 should return <= edges than --depth 5."""
        _step(12, "Depth control")

        table = _find_table_with_downstream(self.cache_file)
        assert table, "No table with downstream edges found"

        result_shallow = _invoke(
            self.config_dir,
            [
                "--json",
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                table,
                "--depth",
                "1",
            ],
        )
        result_deep = _invoke(
            self.config_dir,
            [
                "--json",
                "lineage",
                "show",
                "-l",
                str(self.cache_file),
                "--downstream",
                table,
                "--depth",
                "5",
            ],
        )
        shallow = _json_ok(result_shallow)
        deep = _json_ok(result_deep)

        shallow_count = len(shallow["data"]["edges"])
        deep_count = len(deep["data"]["edges"])
        assert deep_count >= shallow_count, (
            f"depth=5 ({deep_count}) should find >= edges than depth=1 ({shallow_count})"
        )
        print(f"\n  {_GREEN}depth=1: {shallow_count} edges, depth=5: {deep_count} edges{_RESET}")

    # ── Permission check ───────────────────────────────────────────

    def test_13_permission_registered(self) -> None:
        """lineage.info is registered in the permission system."""
        _step(13, "Permission registration check")

        result = _invoke(
            self.config_dir,
            ["--json", "lineage", "info", "-l", str(self.cache_file)],
        )
        # If permission was missing, we'd get exit code 6
        assert result.exit_code == 0
