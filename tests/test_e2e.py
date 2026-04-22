"""Comprehensive end-to-end tests for Keboola Agent CLI.

Exercises the FULL CLI surface against a real (empty) Keboola project:
  - Project CRUD (add / list / status / edit / remove)
  - Storage CRUD (create-bucket / create-table / upload / download / delete)
  - Config operations (list / detail / search / update --set / update --merge / delete)
  - File operations (upload / list / detail / download / tag / delete)
  - Branch lifecycle (list / create / use / reset / merge / delete)
  - Workspace lifecycle (create / list / detail / password / load / query / delete)
  - Component discovery (list / detail / config new scaffold)
  - Job commands (list / detail with filters)
  - Encrypt (values)
  - Permissions (list / show / check)
  - Sync workflow (init / pull / status / diff / push --dry-run)
  - Tool commands (list / call) -- requires keboola-mcp-server
  - Lineage, sharing, doctor, context, version, changelog, init

All resources are prefixed with 'e2e-{run_id}' and cleaned up even on failure.

Requires environment variables:
  - E2E_API_TOKEN: Storage API token
  - E2E_URL: Stack URL (e.g. connection.keboola.com)

Run:
    E2E_API_TOKEN=xxx E2E_URL=connection.keboola.com \
        uv run pytest tests/test_e2e.py -v -s --tb=long
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore

# ---------------------------------------------------------------------------
# Environment & skip logic
# ---------------------------------------------------------------------------

ENV_TOKEN = "E2E_API_TOKEN"
ENV_URL = "E2E_URL"

HAS_CREDENTIALS = os.environ.get(ENV_TOKEN) is not None

skip_without_credentials = pytest.mark.skipif(
    not HAS_CREDENTIALS,
    reason=f"E2E tests require {ENV_TOKEN} environment variable",
)

runner = CliRunner()

# ---------------------------------------------------------------------------
# Unique run identifier (avoids collisions between concurrent runs)
# ---------------------------------------------------------------------------

RUN_ID = f"e2e-{int(time.time())}"

# Component used for creating test configurations (always exists in Keboola)
TEST_COMPONENT_ID = "keboola.ex-db-snowflake"

# ---------------------------------------------------------------------------
# Output formatting constants
# ---------------------------------------------------------------------------

# ANSI colors for terminal output
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_RESET = "\033[0m"
_BOLD = "\033[1m"

# Maximum length for JSON response preview
_MAX_RESPONSE_LEN = 300

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_token(text: str) -> str:
    """Replace any occurrence of the real token in text with a placeholder."""
    token = os.environ.get(ENV_TOKEN, "")
    if token and token in text:
        return text.replace(token, "***TOKEN***")
    return text


def _format_cmd(args: list[str]) -> str:
    """Format CLI args into a readable command string, masking the token."""
    cmd = "kbagent " + " ".join(args)
    return _mask_token(cmd)


def _summarize_json(output: str, max_len: int = _MAX_RESPONSE_LEN) -> str:
    """Pretty-print JSON output, truncated if too long."""
    try:
        data = json.loads(output)
        pretty = json.dumps(data, indent=2, ensure_ascii=False)
        pretty = _mask_token(pretty)
        if len(pretty) > max_len:
            return pretty[:max_len] + f"\n  ... ({len(pretty)} chars total)"
        return pretty
    except (json.JSONDecodeError, TypeError):
        text = _mask_token(output.strip())
        if len(text) > max_len:
            return text[:max_len] + f"... ({len(text)} chars total)"
        return text


def _invoke(config_dir: Path, args: list[str], catch: bool = True) -> Any:
    """Invoke the CLI with a custom config store backed by *config_dir*.

    Prints the command and a response summary for visibility.
    """
    print(f"\n  {_CYAN}$ {_format_cmd(args)}{_RESET}")

    with patch("keboola_agent_cli.cli.ConfigStore") as mock_store_cls:
        mock_store_cls.return_value = ConfigStore(config_dir=config_dir)
        result = runner.invoke(app, args, catch_exceptions=catch)

    # Print result summary
    if result.exit_code == 0:
        status_icon = f"{_GREEN}OK{_RESET}"
    else:
        status_icon = f"{_RED}EXIT {result.exit_code}{_RESET}"

    print(f"  {_DIM}-> {status_icon} {_DIM}({len(result.output)} bytes){_RESET}")

    # Print abbreviated response
    summary = _summarize_json(result.output)
    for line in summary.split("\n"):
        print(f"  {_DIM}   {line}{_RESET}")

    return result


def _json(result) -> dict[str, Any]:
    """Parse CLI result output as JSON, with a clear error if parsing fails."""
    assert result.exit_code == 0, f"Command failed (exit {result.exit_code}):\n{result.output}"
    try:
        return json.loads(result.output)
    except json.JSONDecodeError:
        pytest.fail(f"Output is not valid JSON:\n{result.output}")


def _json_ok(result) -> dict[str, Any]:
    """Parse CLI result as JSON and assert status == 'ok'."""
    data = _json(result)
    assert data.get("status") == "ok", f"Expected status=ok, got: {data}"
    return data


def _step(num: int, title: str, detail: str = "") -> None:
    """Print a visible step marker for -s output."""
    suffix = f" — {detail}" if detail else ""
    print(f"\n{_BOLD}{'=' * 60}")
    print(f"  STEP {num}: {title}{suffix}")
    print(f"{'=' * 60}{_RESET}")


def _create_test_csv(path: Path, rows: int = 5) -> Path:
    """Create a small CSV file for upload testing."""
    csv_path = path / f"{RUN_ID}_data.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "value"])
        for i in range(1, rows + 1):
            writer.writerow([i, f"item_{i}", i * 10])
    return csv_path


def _create_incremental_csv(path: Path, start: int = 6, rows: int = 3) -> Path:
    """Create a CSV file for incremental upload testing."""
    csv_path = path / f"{RUN_ID}_incr_data.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "name", "value"])
        for i in range(start, start + rows):
            writer.writerow([i, f"item_{i}", i * 10])
    return csv_path


def _create_test_file(path: Path, content: str = "hello e2e") -> Path:
    """Create a small text file for file-upload testing."""
    file_path = path / f"{RUN_ID}_file.txt"
    file_path.write_text(content)
    return file_path


def _check_mcp_module() -> bool:
    """Check if keboola-mcp-server is available as a Python module."""
    try:
        result = subprocess.run(
            ["python", "-m", "keboola_mcp_server", "--help"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


# MCP server availability
HAS_MCP_SERVER = shutil.which("keboola_mcp_server") is not None or _check_mcp_module()

skip_without_mcp = pytest.mark.skipif(
    not HAS_MCP_SERVER,
    reason="Tool tests require keboola-mcp-server",
)


def _git(cwd: Path, *args: str) -> str:
    """Run a git command and return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestFullE2E:
    """Comprehensive end-to-end test exercising the entire CLI."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        """Prepare credentials, directories, and API client for cleanup."""
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-proj"

        # Working directories
        self.work_dir = tmp_path / f"kbagent_{RUN_ID}"
        self.work_dir.mkdir()
        self.config_dir = self.work_dir / "config"
        self.config_dir.mkdir()
        self.data_dir = self.work_dir / "data"
        self.data_dir.mkdir()

        # Direct API client for setup / cleanup helpers
        self.api = KeboolaClient(self.url, self.token)

        # Track resources for cleanup
        self._created_buckets: list[str] = []
        self._created_branches: list[int] = []
        self._created_config_ids: list[tuple[str, str]] = []  # (component_id, config_id)
        self._created_file_ids: list[int] = []
        self._created_workspace_ids: list[int] = []

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        """Guarantee cleanup of ALL created resources, even on test failure."""
        yield
        print("\n--- CLEANUP ---")
        # Delete workspaces
        for ws_id in self._created_workspace_ids:
            try:
                self.api.delete_workspace(ws_id)
                print(f"  Deleted workspace {ws_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete workspace {ws_id}: {exc}")

        # Delete configs created via API
        for comp_id, cfg_id in self._created_config_ids:
            try:
                self.api.delete_config(comp_id, cfg_id)
                print(f"  Deleted config {comp_id}/{cfg_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete config {comp_id}/{cfg_id}: {exc}")

        # Delete branches
        for branch_id in self._created_branches:
            try:
                self.api.delete_dev_branch(branch_id)
                print(f"  Deleted branch {branch_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete branch {branch_id}: {exc}")

        # Delete buckets (force to cascade-delete tables)
        for bucket_id in self._created_buckets:
            try:
                self.api.delete_bucket(bucket_id, force=True)
                print(f"  Deleted bucket {bucket_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete bucket {bucket_id}: {exc}")

        # Delete uploaded files
        for file_id in self._created_file_ids:
            try:
                self.api.delete_file(file_id)
                print(f"  Deleted file {file_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete file {file_id}: {exc}")

    # ------------------------------------------------------------------
    # Invoke shorthand
    # ------------------------------------------------------------------
    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def _run_json(self, *args: str) -> dict[str, Any]:
        return _json(self._run(*args))

    def _run_raw(self, *args: str) -> Any:
        """Invoke without --json (for human-readable output testing)."""
        return _invoke(self.config_dir, list(args))

    # ==================================================================
    # THE BIG TEST
    # ==================================================================

    def test_full_cli_e2e(self) -> None:
        """Progressive scenario testing every CLI command group."""

        # ==============================================================
        # PHASE 1: Setup -- offline commands + project registration
        # ==============================================================

        _step(1, "version / changelog / context", "offline commands")
        self._test_offline_commands()

        _step(2, "init", "create local workspace in sub-dir")
        self._test_init()

        _step(3, "project add", "register project")
        self._test_project_add()

        _step(4, "project list + status", "verify connectivity")
        self._test_project_list_and_status()

        _step(5, "doctor", "health check")
        self._test_doctor()

        # ==============================================================
        # PHASE 2: Read empty project
        # ==============================================================

        _step(6, "read empty project", "config list / storage buckets / job list")
        self._test_empty_reads()

        # ==============================================================
        # PHASE 3: Storage CRUD
        # ==============================================================

        _step(7, "storage create-bucket")
        bucket_id = self._test_create_bucket()

        _step(8, "storage buckets + bucket-detail", "verify bucket exists")
        self._test_bucket_listing(bucket_id)

        _step(9, "storage create-table")
        table_id = self._test_create_table(bucket_id)

        _step(10, "storage upload-table", "upload CSV data")
        self._test_upload_table(table_id)

        _step(
            11,
            "storage upload-table --incremental",
            "append rows + verify total",
        )
        self._test_upload_incremental(table_id)

        _step(12, "storage tables + table-detail")
        self._test_table_listing(bucket_id, table_id)

        _step(13, "storage download-table", "data round-trip verification")
        self._test_download_table(table_id)

        _step(14, "storage unload-table", "export to file storage")
        self._test_unload_table(table_id)

        _step(14.1, "storage unload-table --file-type parquet", "Parquet export + sliced download")
        self._test_unload_table_parquet(table_id)

        _step(15, "storage load-file", "upload CSV as file then load into table")
        self._test_load_file(table_id)

        # ==============================================================
        # PHASE 4: Config operations (create via API, test via CLI)
        # ==============================================================

        _step(16, "config create (via API) + CLI list / detail / search")
        config_id = self._test_config_operations()

        _step(17, "config update --set / --dry-run / --name / --configuration")
        self._test_config_update(config_id)

        _step(18, "config update --merge", "partial merge without losing keys")
        self._test_config_merge(config_id)

        _step("18b", "config rename", "rename config via API")
        self._test_config_rename(config_id)

        _step("18c", "config metadata CRUD + set-folder", "metadata round-trip")
        self._test_config_metadata(config_id)

        _step(19, "config new scaffold", "generate boilerplate for component")
        self._test_config_new_scaffold()

        # ==============================================================
        # PHASE 5: Component commands
        # ==============================================================

        _step(20, "component list + detail", "discover components")
        self._test_component_commands()

        # ==============================================================
        # PHASE 6: Workspace lifecycle
        # ==============================================================

        _step(21, "workspace create")
        workspace_id = self._test_workspace_create()

        if workspace_id is not None:
            _step(22, "workspace list")
            self._test_workspace_list(workspace_id)

            _step(23, "workspace detail")
            self._test_workspace_detail(workspace_id)

            _step(24, "workspace password")
            self._test_workspace_password(workspace_id)

            _step(25, "workspace load", "load test table into workspace")
            self._test_workspace_load(workspace_id, table_id)

            _step(26, "workspace query", "run SQL in workspace")
            self._test_workspace_query(workspace_id, table_id)

            _step(27, "workspace delete")
            self._test_workspace_delete(workspace_id)

        _step("27b", "workspace list --orphaned + workspace gc", "orphan GC round-trip")
        self._test_workspace_gc()

        # ==============================================================
        # PHASE 7: Transformation job run (Snowflake SQL)
        # ==============================================================

        _step(28, "transformation setup", "create output bucket + SQL config")
        out_bucket_id, transform_config_id, out_table_id = self._test_transformation_setup(table_id)

        _step(29, "job run --wait", "execute Snowflake transformation")
        job_id = self._test_job_run(transform_config_id)

        _step(30, "job detail", "verify completed job")
        self._test_job_detail(job_id)

        _step(31, "download transformation output", "verify transformed data")
        self._test_transformation_output(out_table_id)

        _step(32, "transformation cleanup")
        self._test_transformation_cleanup(out_bucket_id, transform_config_id)

        # ==============================================================
        # PHASE 7.5: Job terminate (kill long-running / runaway jobs)
        # ==============================================================

        _step(
            32.5,
            "job terminate",
            "spawn sleep job, kill it, verify idempotency",
        )
        self._test_job_terminate()

        # ==============================================================
        # PHASE 8: File operations
        # ==============================================================

        _step(33, "file upload / list / detail / download / tag / delete")
        self._test_file_operations()

        # ==============================================================
        # PHASE 9: Encrypt
        # ==============================================================

        _step(34, "encrypt values")
        self._test_encrypt(config_id)

        # ==============================================================
        # PHASE 10: Branch lifecycle (expanded with merge)
        # ==============================================================

        _step(35, "branch lifecycle", "list / create / use / reset / merge / delete")
        self._test_branch_lifecycle()

        _step(
            36,
            "project description + branch metadata",
            "get/set description + generic metadata CRUD",
        )
        self._test_project_description_and_metadata()

        # ==============================================================
        # PHASE 11: Permissions
        # ==============================================================

        _step(36, "permissions list / show / check", "permission system")
        self._test_permissions()

        # ==============================================================
        # PHASE 12: Sharing & Lineage
        # ==============================================================

        _step(37, "sharing list / lineage show", "read-only checks")
        self._test_sharing_and_lineage()

        # ==============================================================
        # PHASE 12.5: Kai (Keboola AI Assistant)
        # ==============================================================

        _step(38, "kai ping / ask / history", "Keboola AI Assistant")
        self._test_kai_commands()

        # ==============================================================
        # PHASE 13: Job commands (expanded)
        # ==============================================================

        _step(39, "job list + detail", "verify job listing structure")
        self._test_job_commands()

        # ==============================================================
        # PHASE 14: Storage column delete
        # ==============================================================

        _step(40, "storage delete-column", "dry-run + actual delete + verify")
        self._test_delete_column(table_id)

        # ==============================================================
        # PHASE 15: Cleanup
        # ==============================================================

        _step(41, "config delete", "cleanup config via CLI")
        self._test_config_delete(config_id)

        _step(42, "storage delete-table + delete-bucket", "CLI-driven cleanup")
        self._test_storage_cleanup(bucket_id, table_id)

        _step(43, "project edit + remove", "final cleanup")
        self._test_project_edit_and_remove()

        print("\n" + "=" * 60)
        print("  ALL E2E STEPS PASSED")
        print("=" * 60)

    # ==================================================================
    # Step implementations
    # ==================================================================

    def _test_offline_commands(self) -> None:
        """Test version, changelog, context -- no project needed."""
        # version (not JSON, just prints version string)
        result = self._run_raw("version")
        assert result.exit_code == 0
        assert "." in result.output  # should contain a version like "0.18.x"

        # changelog
        result = self._run("changelog")
        assert result.exit_code == 0

        # context
        result = self._run_raw("context")
        assert result.exit_code == 0
        assert "kbagent" in result.output

    def _test_init(self) -> None:
        """Test init command -- creates .kbagent/ in a sub-directory."""
        init_dir = self.work_dir / "init_test"
        init_dir.mkdir()

        # Use a separate config_dir for init (it creates its own workspace)
        init_config_dir = init_dir / "config_for_init"
        init_config_dir.mkdir()

        # Run init from the init_dir by invoking with cwd override
        # The init command uses Path.cwd(), so we patch it
        with patch("keboola_agent_cli.commands.init.Path.cwd", return_value=init_dir):
            result = _invoke(
                init_config_dir,
                ["--json", "init"],
            )
        data = _json_ok(result)
        assert data["data"]["created"] is True
        assert "path" in data["data"]

    def _test_project_add(self) -> None:
        """Add a project and verify the response."""
        data = self._run_ok(
            "project",
            "add",
            "--project",
            self.alias,
            "--url",
            self.url,
            "--token",
            self.token,
        )
        proj = data["data"]
        assert proj["alias"] == self.alias
        assert proj["project_name"]  # non-empty
        assert proj["project_id"] > 0
        # Token must be masked
        assert self.token not in json.dumps(data)

    def _test_project_list_and_status(self) -> None:
        """Verify project appears in list and status is ok."""
        # list
        data = self._run_ok("project", "list")
        aliases = [p["alias"] for p in data["data"]]
        assert self.alias in aliases

        # status
        data = self._run_ok("project", "status", "--project", self.alias)
        status_entry = data["data"][0]
        assert status_entry["alias"] == self.alias
        assert status_entry["status"] == "ok"
        assert status_entry["response_time_ms"] >= 0

    def _test_doctor(self) -> None:
        """Run doctor health check."""
        data = self._run_ok("doctor")
        assert data["data"]["summary"]["healthy"] is True

    def _test_empty_reads(self) -> None:
        """Read operations on a fresh project should return empty lists."""
        # config list
        data = self._run_ok("config", "list", "--project", self.alias)
        assert data["data"]["errors"] == []
        # configs may or may not be empty (some projects have default configs)

        # storage buckets -- filter only our prefix later
        data = self._run_ok("storage", "buckets", "--project", self.alias)
        # Just check structure
        assert "buckets" in data["data"]
        assert "errors" in data["data"]

        # job list
        data = self._run_ok("job", "list", "--project", self.alias, "--limit", "5")
        assert "jobs" in data["data"]
        assert data["data"]["errors"] == []

    def _test_create_bucket(self) -> str:
        """Create a test bucket and return its ID."""
        bucket_name = RUN_ID.replace("-", "_")
        data = self._run_ok(
            "storage",
            "create-bucket",
            "--project",
            self.alias,
            "--stage",
            "in",
            "--name",
            bucket_name,
            "--description",
            "E2E test bucket",
        )
        bucket_id = data["data"]["id"]
        assert bucket_id.startswith("in.c-")
        self._created_buckets.append(bucket_id)
        return bucket_id

    def _test_bucket_listing(self, bucket_id: str) -> None:
        """Verify bucket appears in listings."""
        # buckets
        data = self._run_ok("storage", "buckets", "--project", self.alias)
        bucket_ids = [b["id"] for b in data["data"]["buckets"]]
        assert bucket_id in bucket_ids

        # bucket-detail
        data = self._run_ok(
            "storage",
            "bucket-detail",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
        )
        assert data["data"]["bucket_id"] == bucket_id

    def _test_create_table(self, bucket_id: str) -> str:
        """Create a typed table in the bucket."""
        table_name = f"{RUN_ID.replace('-', '_')}_data"
        data = self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            table_name,
            "--column",
            "id:INTEGER",
            "--column",
            "name:STRING",
            "--column",
            "value:INTEGER",
            "--primary-key",
            "id",
        )
        table_id = data["data"]["table_id"]
        assert table_id
        return table_id

    def _test_upload_table(self, table_id: str) -> None:
        """Upload CSV data to the table."""
        csv_path = _create_test_csv(self.data_dir, rows=5)
        data = self._run_ok(
            "storage",
            "upload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--file",
            str(csv_path),
        )
        assert data["data"]["table_id"] == table_id

    def _test_upload_incremental(self, table_id: str) -> None:
        """Upload additional rows incrementally and verify total count."""
        csv_path = _create_incremental_csv(self.data_dir, start=6, rows=3)
        data = self._run_ok(
            "storage",
            "upload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--file",
            str(csv_path),
            "--incremental",
        )
        assert data["data"]["table_id"] == table_id

        # Download and verify total rows (5 original + 3 incremental = 8)
        output_path = self.data_dir / "incr_verify.csv"
        self._run_ok(
            "storage",
            "download-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--output",
            str(output_path),
        )
        assert output_path.exists()
        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 8, f"Expected 8 rows after incremental upload, got {len(rows)}"

    def _test_table_listing(self, bucket_id: str, table_id: str) -> None:
        """Verify table appears in listings and detail is correct."""
        # tables
        data = self._run_ok(
            "storage",
            "tables",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
        )
        table_ids = [t["id"] for t in data["data"]["tables"]]
        assert table_id in table_ids

        # table-detail
        data = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )
        detail = data["data"]
        assert detail["table_id"] == table_id
        col_names = [c["name"] for c in detail["column_details"]]
        assert "id" in col_names
        assert "name" in col_names
        assert "value" in col_names

    def _test_download_table(self, table_id: str) -> None:
        """Download table data and verify round-trip integrity."""
        output_path = self.data_dir / "downloaded.csv"
        self._run_ok(
            "storage",
            "download-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--output",
            str(output_path),
        )
        assert output_path.exists()

        # Verify content (8 rows after incremental upload)
        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 8

        # Test with --columns and --limit
        limited_path = self.data_dir / "limited.csv"
        self._run_ok(
            "storage",
            "download-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--output",
            str(limited_path),
            "--columns",
            "id",
            "--columns",
            "name",
            "--limit",
            "2",
        )
        assert limited_path.exists()
        with open(limited_path) as f:
            reader = csv.DictReader(f)
            limited_rows = list(reader)
        assert len(limited_rows) == 2
        # Only selected columns
        assert set(limited_rows[0].keys()) == {"id", "name"}

    def _test_unload_table(self, table_id: str) -> None:
        """Unload a table to file storage and optionally download."""
        unload_path = self.data_dir / "unloaded.csv"
        data = self._run_ok(
            "storage",
            "unload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--download",
            "--output",
            str(unload_path),
        )
        result_data = data["data"]
        assert result_data["table_id"] == table_id
        assert result_data["file_id"] > 0
        assert unload_path.exists()

    def _test_unload_table_parquet(self, table_id: str) -> None:
        """Unload a table as sliced Parquet and verify the per-slice download layout.

        Exercises the Storage async export with fileType=parquet and the
        download_sliced_file_to_dir path (each slice saved as its own file,
        _manifest.json sidecar preserved). Concatenation-based download
        would produce an invalid parquet here -- the test fails loudly if
        the wrong path is ever taken.
        """
        out_dir = self.data_dir / "parquet_out"
        data = self._run_ok(
            "storage",
            "unload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--file-type",
            "parquet",
            "--download",
            "--output",
            str(out_dir),
        )
        result = data["data"]
        assert result["table_id"] == table_id
        assert result["file_id"] > 0
        assert result["file_type"] == "parquet"
        assert result["is_sliced"] is True
        assert result["downloaded"] is True
        assert result["slice_count"] >= 1
        assert len(result["slices"]) == result["slice_count"]
        assert out_dir.is_dir()

        manifest = out_dir / "_manifest.json"
        assert manifest.is_file(), "parquet sidecar _manifest.json must be present"

        parquet_files = list(out_dir.glob("*.parquet"))
        assert len(parquet_files) == result["slice_count"], (
            f"expected {result['slice_count']} parquet slices, found {len(parquet_files)}"
        )
        # Every slice should have the parquet magic bytes ("PAR1") at the start
        # and end of the file -- cheap, dependency-free validity check.
        for path in parquet_files:
            raw = path.read_bytes()
            assert len(raw) > 8, f"slice {path.name} is suspiciously small"
            assert raw[:4] == b"PAR1", f"slice {path.name} is missing PAR1 header"
            assert raw[-4:] == b"PAR1", f"slice {path.name} is missing PAR1 footer"

    def _test_load_file(self, table_id: str) -> None:
        """Upload a CSV as a file, then load it into a table via load-file."""
        # Create a CSV file to upload
        csv_path = self.data_dir / f"{RUN_ID}_loadfile.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["id", "name", "value"])
            writer.writerow([100, "loadfile_item", 999])

        # Upload as a Storage file
        data = self._run_ok(
            "storage",
            "file-upload",
            "--project",
            self.alias,
            "--file",
            str(csv_path),
            "--tag",
            f"e2e-loadfile-{RUN_ID}",
        )
        file_id = data["data"]["id"]
        self._created_file_ids.append(file_id)

        # Load file into existing table
        data = self._run_ok(
            "storage",
            "load-file",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--table-id",
            table_id,
            "--incremental",
        )
        assert data["status"] == "ok"

        # Clean up the uploaded file
        self._run_ok(
            "storage",
            "file-delete",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--yes",
        )
        self._created_file_ids.remove(file_id)

    def _test_config_operations(self) -> str:
        """Create a config via API, then test CLI read operations."""
        # Create a test configuration via API (CLI has no config create)
        config_body = self.api.create_config(
            component_id=TEST_COMPONENT_ID,
            name=f"{RUN_ID} Test Config",
            configuration={
                "parameters": {
                    "db": {
                        "host": "test.example.com",
                        "port": 443,
                        "database": "test_db",
                    }
                }
            },
            description="E2E test configuration",
        )
        config_id = str(config_body["id"])
        self._created_config_ids.append((TEST_COMPONENT_ID, config_id))

        # config list -- should find our config
        data = self._run_ok("config", "list", "--project", self.alias)
        config_names = [c["config_name"] for c in data["data"]["configs"]]
        assert f"{RUN_ID} Test Config" in config_names

        # config list with --component-id filter
        data = self._run_ok(
            "config",
            "list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
        )
        our_configs = [c for c in data["data"]["configs"] if c["config_id"] == config_id]
        assert len(our_configs) == 1

        # config detail
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        detail = data["data"]
        assert detail["name"] == f"{RUN_ID} Test Config"
        assert detail["configuration"]["parameters"]["db"]["host"] == "test.example.com"

        # config search
        data = self._run_ok(
            "config",
            "search",
            "--project",
            self.alias,
            "-q",
            RUN_ID,
        )
        matches = data["data"]["matches"]
        assert len(matches) >= 1
        matched_ids = [r["config_id"] for r in matches]
        assert config_id in matched_ids

        # config search with --ignore-case
        data = self._run_ok(
            "config",
            "search",
            "--project",
            self.alias,
            "-q",
            RUN_ID.upper(),
            "--ignore-case",
        )
        assert len(data["data"]["matches"]) >= 1

        return config_id

    def _test_config_update(self, config_id: str) -> None:
        """Test config update with --set, --dry-run, --name, --configuration."""
        # --dry-run first
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--set",
            "parameters.db.host=updated.example.com",
            "--dry-run",
        )
        dry_data = data["data"]
        assert dry_data["dry_run"] is True

        # Apply --set
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--set",
            "parameters.db.host=updated.example.com",
        )

        # Verify the change via config detail
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        assert data["data"]["configuration"]["parameters"]["db"]["host"] == "updated.example.com"
        # Other fields should be preserved
        assert data["data"]["configuration"]["parameters"]["db"]["port"] == 443

        # --set a new nested key
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--set",
            "parameters.db.schema=public",
        )

        # Verify new key exists alongside existing ones
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        db_config = data["data"]["configuration"]["parameters"]["db"]
        assert db_config["schema"] == "public"
        assert db_config["host"] == "updated.example.com"

        # Update name and description
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--name",
            f"{RUN_ID} Updated Config",
            "--description",
            "Updated by E2E test",
        )

        # Verify metadata update
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        assert data["data"]["name"] == f"{RUN_ID} Updated Config"
        assert data["data"]["description"] == "Updated by E2E test"

        # Full configuration replace via --configuration
        full_config = json.dumps(
            {
                "parameters": {
                    "db": {
                        "host": "final.example.com",
                        "port": 5439,
                        "database": "final_db",
                    }
                }
            }
        )
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--configuration",
            full_config,
        )

        # Verify full replace (schema key should be gone)
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        db_config = data["data"]["configuration"]["parameters"]["db"]
        assert db_config["host"] == "final.example.com"
        assert db_config["port"] == 5439
        assert "schema" not in db_config

    def _test_config_merge(self, config_id: str) -> None:
        """Test config update --merge: partial merge without losing existing keys."""
        # Current state: host=final.example.com, port=5439, database=final_db
        # Merge in a new key (timeout) without losing existing ones
        merge_json = json.dumps({"parameters": {"db": {"timeout": 30}}})
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--configuration",
            merge_json,
            "--merge",
        )
        assert data["status"] == "ok"

        # Verify merge: timeout added, existing keys preserved
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        db_config = data["data"]["configuration"]["parameters"]["db"]
        assert db_config["timeout"] == 30, "Merged key 'timeout' should be present"
        assert db_config["host"] == "final.example.com", "Existing 'host' preserved"
        assert db_config["port"] == 5439, "Existing 'port' preserved"
        assert db_config["database"] == "final_db", "Existing 'database' preserved"

    def _test_config_rename(self, config_id: str) -> None:
        """Test config rename: rename a config via API and verify."""
        # Rename the config
        data = self._run_ok(
            "config",
            "rename",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--name",
            "E2E Renamed Config",
        )
        result = data["data"]
        assert result["status"] == "renamed"
        assert result["new_name"] == "E2E Renamed Config"
        assert result["old_name"]  # should have the old name
        assert result["component_id"] == TEST_COMPONENT_ID
        assert result["config_id"] == config_id

        # Verify via config detail that the name actually changed
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        assert data["data"]["name"] == "E2E Renamed Config"

        # Rename back so subsequent tests are not affected
        self._run_ok(
            "config",
            "rename",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--name",
            "E2E Test Config",
        )

    def _test_config_metadata(self, config_id: str) -> None:
        """Config metadata CRUD round-trip + set-folder sugar."""
        custom_key = f"E2E.{RUN_ID}.meta"

        # metadata-list on fresh config -- should be empty
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        initial_entries = data["data"]["metadata"]
        assert isinstance(initial_entries, list)

        # set-metadata -- upsert a custom key
        data = self._run_ok(
            "config",
            "set-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--key",
            custom_key,
            "--value",
            "e2e-value",
        )
        assert data["data"]["key"] == custom_key
        assert data["data"]["value"] == "e2e-value"

        # get-metadata -- verify the value round-trips
        data = self._run_ok(
            "config",
            "get-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--key",
            custom_key,
        )
        assert data["data"]["value"] == "e2e-value"

        # metadata-list -- custom key should now appear
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        entries = data["data"]["metadata"]
        match = next((e for e in entries if e.get("key") == custom_key), None)
        assert match is not None, f"{custom_key} not in metadata list"
        metadata_id = str(match["id"])

        # set-folder -- sugar over KBC.configuration.folderName
        folder_name = f"E2E-folder-{RUN_ID}"
        data = self._run_ok(
            "config",
            "set-folder",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--name",
            folder_name,
        )
        assert data["data"]["folder"] == folder_name

        # verify folder key is visible in list
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        folder_entry = next(
            (e for e in data["data"]["metadata"] if e.get("key") == "KBC.configuration.folderName"),
            None,
        )
        assert folder_entry is not None
        assert folder_entry["value"] == folder_name

        # delete-metadata -- remove the custom key by its ID
        data = self._run_ok(
            "config",
            "delete-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--metadata-id",
            metadata_id,
            "--yes",
        )
        assert metadata_id in data["data"]["message"]

        # verify it's gone
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        remaining_keys = {e.get("key") for e in data["data"]["metadata"]}
        assert custom_key not in remaining_keys

    def _test_config_new_scaffold(self) -> None:
        """Test config new -- generate scaffold for a component."""
        scaffold_dir = self.data_dir / "scaffold"
        scaffold_dir.mkdir()

        data = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            self.alias,
            "--output-dir",
            str(scaffold_dir),
        )
        result = data["data"]
        assert "files_written" in result or "directory" in result

    def _test_component_commands(self) -> None:
        """List components and get detail for one.

        NOTE: component list only returns components that have at least one
        configuration in the project. This test runs AFTER config creation.
        """
        # component list -- now that we have a keboola.ex-db-snowflake config
        data = self._run_ok("component", "list", "--project", self.alias)
        components = data["data"]["components"]
        assert len(components) > 0, "Expected at least one component after config creation"
        comp_ids = [c["component_id"] for c in components]
        assert TEST_COMPONENT_ID in comp_ids

        # component list with --type filter
        data = self._run_ok(
            "component",
            "list",
            "--project",
            self.alias,
            "--type",
            "extractor",
        )
        for c in data["data"]["components"]:
            assert c["component_type"] == "extractor"

        # component detail (uses AI Service)
        data = self._run_ok(
            "component",
            "detail",
            "--component-id",
            TEST_COMPONENT_ID,
            "--project",
            self.alias,
        )
        detail = data["data"]
        assert detail["component_id"] == TEST_COMPONENT_ID
        assert detail["component_type"] == "extractor"

    def _test_workspace_create(self) -> int | None:
        """Create a workspace, return its ID or None if unsupported."""
        result = self._run(
            "workspace",
            "create",
            "--project",
            self.alias,
        )
        if result.exit_code != 0:
            print(
                f"  {_YELLOW}WARN: workspace create failed "
                f"(exit {result.exit_code}), skipping workspace tests{_RESET}"
            )
            return None

        data = _json_ok(result)
        ws_data = data["data"]
        workspace_id = ws_data["workspace_id"]
        assert workspace_id > 0
        self._created_workspace_ids.append(workspace_id)
        return workspace_id

    def _test_workspace_list(self, workspace_id: int) -> None:
        """Verify workspace appears in the list."""
        data = self._run_ok("workspace", "list", "--project", self.alias)
        ws_ids = [w["id"] for w in data["data"]["workspaces"]]
        assert workspace_id in ws_ids

    def _test_workspace_detail(self, workspace_id: int) -> None:
        """Get workspace detail and verify structure."""
        data = self._run_ok(
            "workspace",
            "detail",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )
        detail = data["data"]
        assert detail["workspace_id"] == workspace_id

    def _test_workspace_password(self, workspace_id: int) -> None:
        """Reset workspace password and verify a new password is returned."""
        data = self._run_ok(
            "workspace",
            "password",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )
        assert data["data"]["password"]  # non-empty password

    def _test_workspace_load(self, workspace_id: int, table_id: str) -> None:
        """Load a table into the workspace."""
        data = self._run_ok(
            "workspace",
            "load",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
            "--tables",
            table_id,
        )
        assert data["status"] == "ok"

    def _test_workspace_query(self, workspace_id: int, table_id: str) -> None:
        """Run a SQL query in the workspace and verify result."""
        # Table name in workspace is the last segment of table_id
        ws_table_name = table_id.rsplit(".", 1)[-1]
        sql = f'SELECT COUNT(*) AS cnt FROM "{ws_table_name}"'
        data = self._run_ok(
            "workspace",
            "query",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
            "--sql",
            sql,
        )
        assert data["status"] == "ok"

    def _test_workspace_delete(self, workspace_id: int) -> None:
        """Delete the workspace."""
        data = self._run_ok(
            "workspace",
            "delete",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )
        assert data["status"] == "ok"
        self._created_workspace_ids.remove(workspace_id)

    def _test_workspace_gc(self) -> None:
        """workspace list --orphaned + workspace gc orphan GC round-trip.

        Creates a workspace, then manually deletes the backing sandbox config
        via direct API call to manufacture an orphan, then verifies that:
          - workspace list --orphaned reports it
          - workspace gc --dry-run counts but does not delete
          - workspace gc --yes deletes it
        """
        # Create a workspace to orphan
        result = self._run(
            "workspace",
            "create",
            "--project",
            self.alias,
        )
        if result.exit_code != 0:
            print(
                f"  {_YELLOW}SKIP: workspace create failed "
                f"(exit {result.exit_code}), skipping workspace GC test{_RESET}"
            )
            return

        data = _json_ok(result)
        ws_id = data["data"]["workspace_id"]
        assert ws_id > 0
        # Track for cleanup in case the test fails mid-way
        self._created_workspace_ids.append(ws_id)

        # Retrieve the workspace detail to find its config_id
        ws_data = self.api.get_workspace(ws_id)
        config_id = str(ws_data.get("configurationId") or ws_data.get("config_id") or "")

        if not config_id:
            # Cannot manufacture orphan without config_id; just clean up and skip
            print(f"  {_YELLOW}SKIP: workspace has no config_id, cannot manufacture orphan{_RESET}")
            self.api.delete_workspace(ws_id)
            self._created_workspace_ids.remove(ws_id)
            return

        # Delete the sandbox config directly (bypassing workspace delete) to create an orphan
        try:
            self.api.delete_config("keboola.sandboxes", config_id)
        except Exception as exc:
            print(f"  {_YELLOW}SKIP: could not delete sandbox config: {exc}{_RESET}")
            self.api.delete_workspace(ws_id)
            self._created_workspace_ids.remove(ws_id)
            return

        # workspace list --orphaned -- the workspace should now appear
        data = self._run_ok("workspace", "list", "--project", self.alias, "--orphaned")
        orphan_ids = [w["id"] for w in data["data"]["workspaces"]]
        assert ws_id in orphan_ids, f"ws {ws_id} not listed as orphan; got: {orphan_ids}"

        # workspace gc --dry-run -- count but do not delete
        data = self._run_ok("workspace", "gc", "--project", self.alias, "--dry-run")
        gc_data = data["data"]
        assert gc_data["dry_run"] is True
        would_delete_ids = [w["id"] for w in gc_data.get("would_delete", [])]
        assert ws_id in would_delete_ids

        # workspace gc --yes -- actually delete orphans
        data = self._run_ok("workspace", "gc", "--project", self.alias, "--yes")
        gc_data = data["data"]
        assert gc_data["dry_run"] is False
        deleted_ids = [w["id"] for w in gc_data.get("deleted", [])]
        assert ws_id in deleted_ids

        # workspace no longer tracked for cleanup (GC deleted it)
        if ws_id in self._created_workspace_ids:
            self._created_workspace_ids.remove(ws_id)

    # ------------------------------------------------------------------
    # Transformation job run
    # ------------------------------------------------------------------

    def _test_transformation_setup(self, input_table_id: str) -> tuple[str, str, str]:
        """Create output bucket + Snowflake transformation config.

        Returns (out_bucket_id, transform_config_id, out_table_id).
        """
        # Create output bucket for transformation results
        out_bucket_name = f"{RUN_ID.replace('-', '_')}_out"
        data = self._run_ok(
            "storage",
            "create-bucket",
            "--project",
            self.alias,
            "--stage",
            "out",
            "--name",
            out_bucket_name,
            "--description",
            "E2E transformation output",
        )
        out_bucket_id = data["data"]["id"]
        assert out_bucket_id.startswith("out.c-")
        self._created_buckets.append(out_bucket_id)

        # Derive workspace table name (last segment of table_id)
        ws_input_name = input_table_id.rsplit(".", 1)[-1]
        out_table_id = f"{out_bucket_id}.{RUN_ID.replace('-', '_')}_result"

        # Create Snowflake transformation config via API
        transform_config = {
            "parameters": {
                "blocks": [
                    {
                        "name": "E2E Block",
                        "codes": [
                            {
                                "name": "Transform",
                                "script": [
                                    (
                                        f'CREATE TABLE "{RUN_ID.replace("-", "_")}_result"'
                                        f" AS SELECT"
                                        f' "id",'
                                        f' "name",'
                                        f' CAST("value" AS INTEGER) AS "value",'
                                        f' CAST("value" AS INTEGER) * 2'
                                        f' AS "doubled_value"'
                                        f' FROM "{ws_input_name}"'
                                    )
                                ],
                            }
                        ],
                    }
                ]
            },
            "storage": {
                "input": {
                    "tables": [
                        {
                            "source": input_table_id,
                            "destination": ws_input_name,
                        }
                    ]
                },
                "output": {
                    "tables": [
                        {
                            "source": f"{RUN_ID.replace('-', '_')}_result",
                            "destination": out_table_id,
                        }
                    ]
                },
            },
        }

        config_body = self.api.create_config(
            component_id="keboola.snowflake-transformation",
            name=f"{RUN_ID} SQL Transform",
            configuration=transform_config,
            description="E2E: doubles the value column",
        )
        transform_config_id = str(config_body["id"])
        self._created_config_ids.append(("keboola.snowflake-transformation", transform_config_id))

        return out_bucket_id, transform_config_id, out_table_id

    def _test_job_run(self, transform_config_id: str) -> str:
        """Run the transformation job with --wait and return the job ID."""
        data = self._run_ok(
            "job",
            "run",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            transform_config_id,
            "--wait",
            "--timeout",
            "300",
        )
        job_data = data["data"]
        assert job_data["status"] == "success", (
            f"Job failed with status={job_data['status']}: "
            f"{job_data.get('result', {}).get('message', 'no message')}"
        )
        job_id = str(job_data["id"])
        assert job_id
        return job_id

    def _test_job_detail(self, job_id: str) -> None:
        """Verify job detail for the completed transformation."""
        data = self._run_ok(
            "job",
            "detail",
            "--project",
            self.alias,
            "--job-id",
            job_id,
        )
        detail = data["data"]
        assert detail["status"] == "success"
        assert detail["isFinished"] is True
        assert "keboola.snowflake-transformation" in str(
            detail.get("component", detail.get("operationName", ""))
        )

    def _test_transformation_output(self, out_table_id: str) -> None:
        """Download the transformation output and verify doubled values."""
        output_path = self.data_dir / "transform_output.csv"
        self._run_ok(
            "storage",
            "download-table",
            "--project",
            self.alias,
            "--table-id",
            out_table_id,
            "--output",
            str(output_path),
        )
        assert output_path.exists()

        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        # 5 original + 3 incremental + 1 from load-file = 9 rows
        assert len(rows) >= 8, f"Expected at least 8 rows, got {len(rows)}"

        # Verify transformation: doubled_value == value * 2
        for row in rows:
            value = int(row["value"])
            doubled = int(row["doubled_value"])
            assert doubled == value * 2, (
                f"Row id={row['id']}: value={value}, "
                f"expected doubled_value={value * 2}, got {doubled}"
            )

    def _test_job_terminate(self) -> None:
        """End-to-end coverage for `kbagent job terminate`.

        Spawns a python-transformation-v2 job that would sleep for 10 minutes,
        terminates it via CLI, confirms the buckets behave as documented:
          - killed (200)
          - already_finished on re-terminate (400 "not in killable states")
          - not_found on a bogus ID (500/body-404 disambiguated via GET)

        Any leftover config is cleaned up at the end.
        """
        # Sleep transformation - long enough that we always catch it in a killable state
        kill_config_body = self.api.create_config(
            component_id="keboola.python-transformation-v2",
            name=f"{RUN_ID} kill-test",
            configuration={
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [
                                {
                                    "name": "sleep",
                                    "script": ["import time", "time.sleep(600)"],
                                }
                            ],
                        }
                    ]
                }
            },
            description="E2E: spawned only to be terminated",
        )
        kill_config_id = str(kill_config_body["id"])
        self._created_config_ids.append(("keboola.python-transformation-v2", kill_config_id))

        # Spawn without --wait (we want it still alive to kill)
        data = self._run_ok(
            "job",
            "run",
            "--project",
            self.alias,
            "--component-id",
            "keboola.python-transformation-v2",
            "--config-id",
            kill_config_id,
        )
        job_id = str(data["data"]["id"])
        assert job_id

        # Dry-run first: should not touch the job
        dry = self._run_ok(
            "job",
            "terminate",
            "--project",
            self.alias,
            "--job-id",
            job_id,
            "--dry-run",
        )["data"]
        assert dry["dry_run"] is True
        assert dry["would_terminate"] == [job_id]
        assert dry["killed"] == []

        # Real terminate
        killed_result = self._run_ok(
            "job",
            "terminate",
            "--project",
            self.alias,
            "--job-id",
            job_id,
            "--yes",
        )["data"]
        assert len(killed_result["killed"]) == 1
        assert killed_result["killed"][0]["id"] == job_id
        assert killed_result["killed"][0]["desiredStatus"] == "terminating"
        assert killed_result["failed"] == []

        # Poll until job reaches a terminal state (isFinished=True)
        for _ in range(40):
            detail = self._run_ok(
                "job",
                "detail",
                "--project",
                self.alias,
                "--job-id",
                job_id,
            )["data"]
            if detail["isFinished"]:
                break
            time.sleep(2)
        else:
            raise AssertionError(f"Job {job_id} did not reach terminal state within 80s")
        assert detail["status"] in {"cancelled", "terminated"}

        # Idempotency: re-terminate should hit the already_finished bucket
        idemp = self._run_ok(
            "job",
            "terminate",
            "--project",
            self.alias,
            "--job-id",
            job_id,
            "--yes",
        )["data"]
        assert idemp["killed"] == []
        assert len(idemp["already_finished"]) == 1
        assert idemp["already_finished"][0]["id"] == job_id

        # Bogus ID should be classified as not_found (via GET fallback)
        bogus = self._run_ok(
            "job",
            "terminate",
            "--project",
            self.alias,
            "--job-id",
            "99999999999999",
            "--yes",
        )["data"]
        assert bogus["not_found"] == ["99999999999999"]
        assert bogus["failed"] == []

        # Cleanup the config now (don't leak resources even if later phases fail)
        self.api.delete_config("keboola.python-transformation-v2", kill_config_id)
        self._created_config_ids.remove(("keboola.python-transformation-v2", kill_config_id))

    def _test_transformation_cleanup(self, out_bucket_id: str, transform_config_id: str) -> None:
        """Clean up transformation resources via CLI."""
        # Delete transformation config
        self._run_ok(
            "config",
            "delete",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            transform_config_id,
        )
        self._created_config_ids.remove(("keboola.snowflake-transformation", transform_config_id))

        # Delete output bucket (--force to cascade delete output table)
        self._run_ok(
            "storage",
            "delete-bucket",
            "--project",
            self.alias,
            "--bucket-id",
            out_bucket_id,
            "--force",
            "--yes",
        )
        self._created_buckets.remove(out_bucket_id)

    def _test_file_operations(self) -> None:
        """Test the full file lifecycle: upload, list, detail, download, tag, delete."""
        # Create a test file
        test_file = _create_test_file(self.data_dir, content=f"E2E test data {RUN_ID}")

        # file-upload
        data = self._run_ok(
            "storage",
            "file-upload",
            "--project",
            self.alias,
            "--file",
            str(test_file),
            "--tag",
            f"e2e-{RUN_ID}",
            "--tag",
            "test",
        )
        file_id = data["data"]["id"]
        self._created_file_ids.append(file_id)
        assert file_id > 0

        # files (list)
        data = self._run_ok(
            "storage",
            "files",
            "--project",
            self.alias,
            "--tag",
            f"e2e-{RUN_ID}",
        )
        file_ids = [f["id"] for f in data["data"]["files"]]
        assert file_id in file_ids

        # file-detail
        data = self._run_ok(
            "storage",
            "file-detail",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
        )
        assert data["data"]["id"] == file_id
        assert f"e2e-{RUN_ID}" in data["data"]["tags"]

        # file-download
        download_path = self.data_dir / "downloaded_file.txt"
        data = self._run_ok(
            "storage",
            "file-download",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--output",
            str(download_path),
        )
        assert download_path.exists()
        downloaded_content = download_path.read_text()
        assert RUN_ID in downloaded_content

        # file-tag: add a tag
        data = self._run_ok(
            "storage",
            "file-tag",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--add",
            "extra-tag",
        )

        # Verify tag was added
        data = self._run_ok(
            "storage",
            "file-detail",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
        )
        assert "extra-tag" in data["data"]["tags"]

        # file-tag: remove a tag
        data = self._run_ok(
            "storage",
            "file-tag",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--remove",
            "extra-tag",
        )

        # file-delete (with --dry-run first)
        data = self._run_ok(
            "storage",
            "file-delete",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--dry-run",
        )
        assert file_id in data["data"]["would_delete"]

        # Actual delete
        data = self._run_ok(
            "storage",
            "file-delete",
            "--project",
            self.alias,
            "--file-id",
            str(file_id),
            "--yes",
        )
        assert file_id in data["data"]["deleted"]
        # Remove from cleanup list since we already deleted it
        self._created_file_ids.remove(file_id)

    def _test_encrypt(self, config_id: str) -> None:
        """Test encrypting values."""
        input_json = json.dumps({"#password": "secret123", "#api_key": "key456"})
        data = self._run_ok(
            "encrypt",
            "values",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--input",
            input_json,
        )
        encrypted = data["data"]
        # Encrypted values should start with KBC::ProjectSecure:: or similar
        assert "#password" in encrypted
        assert "#api_key" in encrypted
        assert encrypted["#password"] != "secret123"  # must be encrypted
        assert encrypted["#api_key"] != "key456"
        assert encrypted["#password"].startswith("KBC::")

    def _test_branch_lifecycle(self) -> None:
        """Test branch create, list, use, reset, merge (or delete)."""
        # branch list -- should only have main
        data = self._run_ok("branch", "list", "--project", self.alias)
        branches = data["data"]["branches"]
        # Main branch always exists
        assert len(branches) >= 1

        # branch create
        branch_name = f"{RUN_ID}-test-branch"
        data = self._run_ok(
            "branch",
            "create",
            "--project",
            self.alias,
            "--name",
            branch_name,
            "--description",
            "E2E test branch",
        )
        branch_data = data["data"]
        branch_id = branch_data["branch_id"]
        assert branch_id > 0
        assert branch_data["branch_name"] == branch_name
        assert branch_data["activated"] is True
        self._created_branches.append(branch_id)
        # Branch create auto-activates -- reset so further tests use main
        self._run_ok("branch", "reset", "--project", self.alias)

        # branch list -- should now include our branch
        data = self._run_ok("branch", "list", "--project", self.alias)
        branch_names = [b["name"] for b in data["data"]["branches"]]
        assert branch_name in branch_names

        # branch use -- activate the dev branch
        data = self._run_ok(
            "branch",
            "use",
            "--project",
            self.alias,
            "--branch",
            str(branch_id),
        )

        # Verify: project status should show active branch
        data = self._run_ok("project", "status", "--project", self.alias)
        status = data["data"][0]
        assert status["active_branch_id"] == branch_id

        # Storage commands should work in branch context
        data = self._run_ok("storage", "buckets", "--project", self.alias)
        assert data["data"]["errors"] == []

        # job run should respect active branch (issue #170)
        # Find any config to run a quick job (no --wait)
        cfg_data = self._run_ok("config", "list", "--project", self.alias)
        configs = cfg_data["data"]["configs"]
        if configs:
            test_cfg = configs[0]
            job_data = self._run_ok(
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                test_cfg["component_id"],
                "--config-id",
                test_cfg["config_id"],
            )
            job = job_data["data"]
            assert str(job.get("branchId")) == str(branch_id), (
                f"job run with active branch: expected branchId={branch_id}, "
                f"got {job.get('branchId')}"
            )

        # branch reset -- deactivate the dev branch
        data = self._run_ok("branch", "reset", "--project", self.alias)

        # Verify: project status should show no active branch
        data = self._run_ok("project", "status", "--project", self.alias)
        status = data["data"][0]
        assert status["active_branch_id"] is None

        # Try branch merge
        merge_result = self._run(
            "branch",
            "merge",
            "--project",
            self.alias,
            "--branch",
            str(branch_id),
        )
        # branch merge returns a URL for UI-based merge; it doesn't
        # auto-merge via API. We verify the command succeeds, then delete.
        if merge_result.exit_code == 0:
            merge_data = json.loads(merge_result.output)
            assert merge_data["status"] == "ok"
            # The response contains a URL to the branch overview
            assert "url" in merge_data["data"] or "message" in merge_data["data"]

        # Clean up: delete the branch
        self._run_ok(
            "branch",
            "delete",
            "--project",
            self.alias,
            "--branch",
            str(branch_id),
        )
        self._created_branches.remove(branch_id)

        # Verify branch is gone
        data = self._run_ok("branch", "list", "--project", self.alias)
        branch_ids = [b["id"] for b in data["data"]["branches"]]
        assert branch_id not in branch_ids

    def _test_project_description_and_metadata(self) -> None:
        """Test project description + branch metadata CRUD round-trip.

        Uses the default branch so the dashboard reflects the change. Captures
        the original description up-front and restores it at the end to avoid
        polluting the shared E2E project.
        """
        # Capture original description so we can restore it
        data = self._run_ok("project", "description-get", "--project", self.alias)
        original_desc = data["data"]["description"]

        marker = f"# E2E {RUN_ID}\n\nTemporary project description."

        try:
            # set via --text
            data = self._run_ok(
                "project",
                "description-set",
                "--project",
                self.alias,
                "--text",
                marker,
            )
            assert "updated" in data["data"]["message"].lower()

            # get roundtrip
            data = self._run_ok("project", "description-get", "--project", self.alias)
            assert data["data"]["description"] == marker

            # generic branch metadata-list should include KBC.projectDescription
            data = self._run_ok(
                "branch",
                "metadata-list",
                "--project",
                self.alias,
                "--branch",
                "default",
            )
            entries = data["data"]["metadata"]
            match = next(
                (e for e in entries if e.get("key") == "KBC.projectDescription"),
                None,
            )
            assert match is not None, "KBC.projectDescription not in metadata list"
            assert match["value"] == marker

            # generic branch metadata-get by key
            data = self._run_ok(
                "branch",
                "metadata-get",
                "--project",
                self.alias,
                "--key",
                "KBC.projectDescription",
                "--branch",
                "default",
            )
            assert data["data"]["value"] == marker

            # set via branch metadata-set (custom key we can then delete by id)
            custom_key = f"E2E.{RUN_ID}.custom"
            data = self._run_ok(
                "branch",
                "metadata-set",
                "--project",
                self.alias,
                "--key",
                custom_key,
                "--text",
                "e2e-value",
                "--branch",
                "default",
            )
            assert data["data"]["key"] == custom_key

            # find the new entry ID so we can delete it
            data = self._run_ok(
                "branch",
                "metadata-list",
                "--project",
                self.alias,
                "--branch",
                "default",
            )
            custom_entry = next(e for e in data["data"]["metadata"] if e.get("key") == custom_key)
            metadata_id = int(custom_entry["id"])

            # delete by ID
            data = self._run_ok(
                "branch",
                "metadata-delete",
                "--project",
                self.alias,
                "--metadata-id",
                str(metadata_id),
                "--branch",
                "default",
            )
            assert str(metadata_id) in data["data"]["message"]

            # verify delete
            data = self._run_ok(
                "branch",
                "metadata-list",
                "--project",
                self.alias,
                "--branch",
                "default",
            )
            keys_after = {e.get("key") for e in data["data"]["metadata"]}
            assert custom_key not in keys_after
        finally:
            # Restore original description so we don't leave e2e markers behind
            self._run_ok(
                "project",
                "description-set",
                "--project",
                self.alias,
                "--text",
                original_desc,
            )

    def _test_permissions(self) -> None:
        """Test permissions list, show, and check commands."""
        # permissions list -- returns array of operations
        data = self._run_ok("permissions", "list")
        operations = data["data"]
        assert isinstance(operations, list)
        assert len(operations) > 0
        # Each operation should have required fields
        op = operations[0]
        assert "name" in op
        assert "category" in op

        # permissions show -- no policy set, should show inactive
        data = self._run_ok("permissions", "show")
        assert data["data"]["active"] is False

        # permissions check -- without policy, everything should be allowed
        data = self._run_ok("permissions", "check", "branch.delete")
        assert data["data"]["operation"] == "branch.delete"
        assert data["data"]["allowed"] is True

    def _test_sharing_and_lineage(self) -> None:
        """Test sharing list and lineage show (read-only, may be empty)."""
        # sharing list
        data = self._run_ok("sharing", "list", "--project", self.alias)
        assert "shared_buckets" in data["data"] or "errors" in data["data"]

        # lineage show
        data = self._run_ok("lineage", "show", "--project", self.alias)
        # Lineage may be empty on a single-project setup
        assert data["status"] == "ok"

    def _test_kai_commands(self) -> None:
        """Test Kai AI Assistant commands (gracefully skip if not available)."""
        # kai ping — check if Kai is available for this project
        result = self._run("kai", "ping", "--project", self.alias)
        if result.exit_code != 0:
            output = result.output
            if "KAI_NOT_ENABLED" in output or "KAI_ERROR" in output:
                print(
                    f"  {_YELLOW}SKIP: Kai not available for this project "
                    f"(exit {result.exit_code}){_RESET}"
                )
                return
            # Unexpected error — fail the test
            assert result.exit_code == 0, f"kai ping failed unexpectedly: {result.output}"

        # Ping succeeded — verify structure
        ping_data = json.loads(result.output)
        assert ping_data["status"] == "ok"
        assert "timestamp" in ping_data["data"]
        assert "mcp_status" in ping_data["data"]

        # kai ask — one-shot question
        result = self._run(
            "kai",
            "ask",
            "--project",
            self.alias,
            "-m",
            "Reply with just the word OK",
        )
        if result.exit_code != 0:
            # Auth issue (e.g. token type) — skip remaining kai tests
            print(
                f"  {_YELLOW}SKIP: kai ask failed "
                f"(exit {result.exit_code}), skipping chat/history{_RESET}"
            )
            return

        ask_data = json.loads(result.output)
        assert ask_data["status"] == "ok"
        assert "response" in ask_data["data"]
        assert "chat_id" in ask_data["data"]
        assert len(ask_data["data"]["response"]) > 0

        # kai history — list recent chats (at least the one we just created)
        data = self._run_ok("kai", "history", "--project", self.alias, "--limit", "5")
        assert "chats" in data["data"]
        # We just chatted, so there should be at least 1
        assert len(data["data"]["chats"]) >= 1

    def _test_job_commands(self) -> None:
        """Verify job listing structure and detail (if jobs exist)."""
        # job list
        data = self._run_ok(
            "job",
            "list",
            "--project",
            self.alias,
            "--limit",
            "5",
        )
        assert "jobs" in data["data"]
        assert "errors" in data["data"]
        assert data["data"]["errors"] == []

        # job list with component filter
        data = self._run_ok(
            "job",
            "list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--limit",
            "5",
        )
        assert "jobs" in data["data"]

        # If any jobs exist, get detail for the first one
        jobs = data["data"]["jobs"]
        if jobs:
            job_id = str(jobs[0]["id"])
            detail_data = self._run_ok(
                "job",
                "detail",
                "--project",
                self.alias,
                "--job-id",
                job_id,
            )
            assert detail_data["data"]["id"]

    def _test_config_delete(self, config_id: str) -> None:
        """Delete the test config via CLI."""
        data = self._run_ok(
            "config",
            "delete",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        assert data["data"]["config_id"] == config_id
        # Remove from cleanup since we deleted via CLI
        self._created_config_ids.remove((TEST_COMPONENT_ID, config_id))

    def _test_delete_column(self, table_id: str) -> None:
        """Delete a column from a table: dry-run, actual delete, verify."""
        # Verify the table has 'value' column before we delete it
        data = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )
        columns_before = data["data"]["columns"]
        assert "value" in columns_before, f"Expected 'value' column, got {columns_before}"

        # delete-column dry-run
        data = self._run_ok(
            "storage",
            "delete-column",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--column",
            "value",
            "--dry-run",
        )
        assert data["data"]["dry_run"] is True
        assert "value" in data["data"]["would_delete"]
        assert data["data"]["table_id"] == table_id

        # delete-column (actual)
        data = self._run_ok(
            "storage",
            "delete-column",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--column",
            "value",
            "--yes",
        )
        assert "value" in data["data"]["deleted"]
        assert data["data"]["failed"] == []
        assert data["data"]["table_id"] == table_id

        # Verify the column is gone
        data = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )
        columns_after = data["data"]["columns"]
        assert "value" not in columns_after, (
            f"'value' column should be deleted, got {columns_after}"
        )
        assert "id" in columns_after
        assert "name" in columns_after

    def _test_storage_cleanup(self, bucket_id: str, table_id: str) -> None:
        """Delete table and bucket via CLI commands."""
        # delete-table (dry-run first)
        data = self._run_ok(
            "storage",
            "delete-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--dry-run",
        )
        assert table_id in data["data"]["would_delete"]

        # delete-table (actual)
        data = self._run_ok(
            "storage",
            "delete-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--yes",
        )
        assert table_id in data["data"]["deleted"]

        # delete-bucket (dry-run first)
        data = self._run_ok(
            "storage",
            "delete-bucket",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--dry-run",
        )
        assert bucket_id in data["data"]["would_delete"]

        # delete-bucket (actual)
        data = self._run_ok(
            "storage",
            "delete-bucket",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--yes",
        )
        assert bucket_id in data["data"]["deleted"]
        self._created_buckets.remove(bucket_id)

    def _test_project_edit_and_remove(self) -> None:
        """Edit project URL, then remove it."""
        # project edit -- change URL back to same (just verify command works)
        data = self._run_ok(
            "project",
            "edit",
            "--project",
            self.alias,
            "--url",
            self.url,
        )
        assert data["data"]["alias"] == self.alias

        # project remove
        data = self._run_ok("project", "remove", "--project", self.alias)
        assert data["data"]["message"]

        # Verify project is gone
        data = self._run_ok("project", "list")
        remaining = [p["alias"] for p in data["data"]]
        assert self.alias not in remaining


# ---------------------------------------------------------------------------
# Error handling tests (separate from the main flow)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EErrorHandling:
    """Test error paths and edge cases."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def test_add_with_invalid_token(self) -> None:
        """Adding a project with an invalid token returns exit code 3."""
        result = self._run(
            "project",
            "add",
            "--project",
            "bad-project",
            "--url",
            self.url,
            "--token",
            "000-definitely-invalid-token",
        )
        assert result.exit_code == 3
        data = json.loads(result.output)
        assert data["status"] == "error"

    def test_status_of_nonexistent_project(self) -> None:
        """Status of a project that doesn't exist returns exit code 5."""
        result = self._run("project", "status", "--project", "nonexistent")
        assert result.exit_code == 5

    def test_remove_nonexistent_project(self) -> None:
        """Removing a nonexistent project returns exit code 5."""
        result = self._run("project", "remove", "--project", "nonexistent")
        assert result.exit_code == 5

    def test_config_detail_nonexistent(self) -> None:
        """Config detail for nonexistent config returns error."""
        # First add a valid project
        self._run(
            "project",
            "add",
            "--project",
            "err-test",
            "--url",
            self.url,
            "--token",
            self.token,
        )
        result = self._run(
            "config",
            "detail",
            "--project",
            "err-test",
            "--component-id",
            "keboola.ex-db-snowflake",
            "--config-id",
            "999999999",
        )
        assert result.exit_code != 0

    def test_download_nonexistent_table(self) -> None:
        """Downloading a nonexistent table returns error."""
        self._run(
            "project",
            "add",
            "--project",
            "err-test2",
            "--url",
            self.url,
            "--token",
            self.token,
        )
        result = self._run(
            "storage",
            "download-table",
            "--project",
            "err-test2",
            "--table-id",
            "in.c-nonexistent.nonexistent",
        )
        assert result.exit_code != 0

    def test_delete_nonexistent_bucket(self) -> None:
        """Deleting a nonexistent bucket returns error."""
        self._run(
            "project",
            "add",
            "--project",
            "err-test3",
            "--url",
            self.url,
            "--token",
            self.token,
        )
        result = self._run(
            "storage",
            "delete-bucket",
            "--project",
            "err-test3",
            "--bucket-id",
            "in.c-nonexistent-bucket-xyz",
            "--yes",
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# JSON output consistency tests
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EJsonConsistency:
    """Verify that all commands produce valid JSON with --json flag."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-json"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        # Add project
        _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def test_all_read_commands_return_valid_json(self) -> None:
        """Every read command should return parseable JSON with status field."""
        commands = [
            ["project", "list"],
            ["project", "status", "--project", self.alias],
            ["config", "list", "--project", self.alias],
            ["storage", "buckets", "--project", self.alias],
            ["job", "list", "--project", self.alias, "--limit", "1"],
            ["component", "list", "--project", self.alias],
            ["branch", "list", "--project", self.alias],
            ["sharing", "list", "--project", self.alias],
            ["lineage", "show", "--project", self.alias],
            ["doctor"],
            ["permissions", "list"],
            ["permissions", "show"],
        ]
        for cmd in commands:
            result = self._run(*cmd)
            assert result.exit_code == 0, (
                f"Command {' '.join(cmd)} failed (exit {result.exit_code}): {result.output}"
            )
            try:
                data = json.loads(result.output)
            except json.JSONDecodeError:
                pytest.fail(
                    f"Command {' '.join(cmd)} did not return valid JSON: {result.output[:200]}"
                )
            assert "status" in data, f"Command {' '.join(cmd)} missing 'status' key: {data}"

    def test_token_never_appears_in_any_output(self) -> None:
        """The full token should never appear in any command output."""
        commands = [
            ["project", "list"],
            ["project", "status", "--project", self.alias],
            ["doctor"],
        ]
        for cmd in commands:
            result = self._run(*cmd)
            assert self.token not in result.output, (
                f"Full token leaked in output of: {' '.join(cmd)}"
            )


# ---------------------------------------------------------------------------
# Sync workflow tests
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2ESyncWorkflow:
    """Test sync init/pull/diff/status/push in a temp git repo."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        """Set up config dir, project dir (as git repo), and register project."""
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-sync"

        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.project_dir = tmp_path / "project"
        self.project_dir.mkdir()

        # Register the project
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

        # Initialize git repo
        _git(self.project_dir, "init")
        _git(self.project_dir, "config", "user.email", "e2e@test.local")
        _git(self.project_dir, "config", "user.name", "E2E Test")
        _git(
            self.project_dir,
            "commit",
            "--allow-empty",
            "-m",
            "init",
        )

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_sync_workflow(self) -> None:
        """Full sync lifecycle: init, pull, status, diff, push --dry-run."""

        # 1. sync init
        _step(1, "sync init")
        data = self._run_ok(
            "sync",
            "init",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
        )
        result = data["data"]
        assert result["project_alias"] == self.alias

        # 2. sync pull
        _step(2, "sync pull")
        data = self._run_ok(
            "sync",
            "pull",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
        )
        pull_result = data["data"]
        # Should have configs_pulled key (may be 0 on empty project)
        assert "configs_pulled" in pull_result

        # Commit pulled files so status/diff have a baseline
        _git(self.project_dir, "add", "-A")
        _git(self.project_dir, "commit", "-m", "pulled configs")

        # 3. sync status
        _step(3, "sync status")
        data = self._run_ok(
            "sync",
            "status",
            "--directory",
            str(self.project_dir),
        )
        assert data["status"] == "ok"

        # 4. sync diff
        _step(4, "sync diff")
        data = self._run_ok(
            "sync",
            "diff",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
        )
        assert data["status"] == "ok"

        # 5. sync push --dry-run
        _step(5, "sync push --dry-run")
        data = self._run_ok(
            "sync",
            "push",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
            "--dry-run",
        )
        assert data["status"] == "ok"

    def test_sync_push_variable_row_round_trip(self) -> None:
        """PR1 P0-1 acceptance: edit a keboola.variables values row, push, pull back.

        Locks the row-deploy contract: after sync push, the API's row
        ``configuration`` dict must equal what we wrote locally (byte-equal
        deep comparison). Creates + cleans up a dedicated ``keboola.variables``
        config + row so the test is idempotent across runs.
        """
        import yaml as _yaml

        from keboola_agent_cli.client import KeboolaClient
        from keboola_agent_cli.constants import CONFIG_FILENAME

        component_id = "keboola.variables"
        var_cfg: dict = {}
        row_id: str = ""

        # Wrap setup + body in a single try/finally so the cleanup still
        # runs if create_config_row fails after create_config succeeded --
        # otherwise we leak a variables config on every failed run.
        try:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                var_cfg = api.create_config(
                    component_id=component_id,
                    name=f"e2e-pr1-{RUN_ID}",
                    description="FIIA row-push E2E fixture",
                    configuration={
                        "variables": [
                            {"name": "year_start", "type": "string"},
                            {"name": "region", "type": "string"},
                        ]
                    },
                )
                row = api.create_config_row(
                    component_id=component_id,
                    config_id=var_cfg["id"],
                    name="main",
                    configuration={
                        "values": [
                            {"name": "year_start", "value": "2016"},
                            {"name": "region", "value": "eu"},
                        ]
                    },
                )
                row_id = row["id"]

            # --- step A: sync init + pull ---
            _step("6a", "sync init + pull (row-push setup)")
            self._run_ok(
                "sync",
                "init",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
            )
            self._run_ok(
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
            )

            # --- step B: locate the row YAML file on disk ---
            row_files = [
                p
                for p in self.project_dir.rglob(CONFIG_FILENAME)
                if "rows" in p.relative_to(self.project_dir).parts
                and _yaml.safe_load(p.read_text(encoding="utf-8")).get("_keboola", {}).get("row_id")
                == row_id
            ]
            assert len(row_files) == 1, f"Row YAML not found after pull. Candidates: {row_files}"
            row_file = row_files[0]

            # --- step C: edit the row values locally (FIIA's primary use case) ---
            _step("6b", "edit values row locally + sync push")
            local_data = _yaml.safe_load(row_file.read_text(encoding="utf-8"))
            # Hoisted top-level `values` key (see config_format.ROW_HOIST_COMPONENTS).
            assert "values" in local_data, f"Expected hoisted 'values' key: {list(local_data)}"
            local_data["values"] = [
                {"name": "year_start", "value": "2025"},
                {"name": "region", "value": "us-west"},
            ]
            row_file.write_text(
                _yaml.dump(local_data, default_flow_style=False, sort_keys=False),
                encoding="utf-8",
            )

            push_data = self._run_ok(
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
            )
            push_result = push_data["data"]
            assert push_result["status"] == "pushed"
            assert push_result["updated"] == 1, f"Expected 1 update (the row), got {push_result}"

            # --- step D: pull fresh state back and assert byte-equal ---
            _step("6c", "sync pull + verify row round-trip byte-equal")
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                remote_row = api.get_config_detail(
                    component_id=component_id,
                    config_id=var_cfg["id"],
                )
                remote_rows = remote_row.get("rows", [])
                updated_row = next(r for r in remote_rows if r["id"] == row_id)
                assert updated_row["configuration"] == {
                    "values": [
                        {"name": "year_start", "value": "2025"},
                        {"name": "region", "value": "us-west"},
                    ]
                }
        finally:
            # --- cleanup: delete the variables config we created ---
            # Guard on var_cfg["id"] so a failure before create_config returned
            # doesn't turn into a KeyError inside the cleanup handler.
            cfg_id = var_cfg.get("id") if var_cfg else None
            if cfg_id:
                try:
                    with KeboolaClient(stack_url=self.url, token=self.token) as api:
                        api.delete_config(component_id=component_id, config_id=cfg_id)
                except Exception as exc:
                    print(f"  [cleanup] Failed to delete {component_id}/{cfg_id}: {exc}")

    def test_config_variables_round_trip(self) -> None:
        """CLAUDE.md rule 16: every new CLI command needs an E2E test.

        Exercises ``config variables-{set,get,clear}`` end-to-end against a
        real parent config: auto-create path, merge path, replace path,
        readback, and clear. Locks the happy-path contract so agents can
        trust the response shape from real Storage API responses (not just
        mocks). Cleans up both the parent test config and the auto-created
        ``keboola.variables`` sibling.
        """
        parent_cfg: dict = {}
        auto_vars_id: str | None = None
        try:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                parent_cfg = api.create_config(
                    component_id=TEST_COMPONENT_ID,
                    name=f"{RUN_ID}-vars-parent",
                    description="E2E variables round-trip parent config",
                    configuration={"parameters": {"db": {"host": "test.example.com"}}},
                )
            parent_id = str(parent_cfg["id"])

            # --- step A: variables-set (AUTO-CREATE) ---
            _step("7a", "config variables-set (auto-create path)")
            data = self._run_ok(
                "config",
                "variables-set",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
                "--var",
                "year_start=2016",
                "--var",
                "region=eu",
            )["data"]
            assert data["action"] == "created"
            assert data["values"] == {"year_start": "2016", "region": "eu"}
            auto_vars_id = data["variables_id"]
            assert auto_vars_id, "auto-create path must return a variables_id"

            # --- step B: variables-get (readback) ---
            _step("7b", "config variables-get (readback after set)")
            data = self._run_ok(
                "config",
                "variables-get",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
            )["data"]
            assert data["linked"] is True
            assert data["values"] == {"year_start": "2016", "region": "eu"}
            assert data["variables_id"] == auto_vars_id

            # --- step C: variables-set (MERGE) ---
            _step("7c", "config variables-set (merge: adds year_end)")
            data = self._run_ok(
                "config",
                "variables-set",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
                "--var",
                "year_end=2024",
            )["data"]
            assert data["action"] == "updated"
            assert data["values"] == {
                "year_start": "2016",
                "region": "eu",
                "year_end": "2024",
            }

            # --- step D: variables-set --replace ---
            _step("7d", "config variables-set --replace (drops prior keys)")
            data = self._run_ok(
                "config",
                "variables-set",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
                "--var",
                "only_key=only_value",
                "--replace",
            )["data"]
            assert data["values"] == {"only_key": "only_value"}

            # --- step E: variables-clear ---
            _step("7e", "config variables-clear (unlink)")
            data = self._run_ok(
                "config",
                "variables-clear",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
                "--yes",
            )["data"]
            assert data["was_linked"] is True
            assert data["unlinked_variables_id"] == auto_vars_id

            # Post-clear: variables-get must report linked=False
            data = self._run_ok(
                "config",
                "variables-get",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                parent_id,
            )["data"]
            assert data["linked"] is False
            assert data["values"] == {}
        finally:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                parent_id_cleanup = parent_cfg.get("id") if parent_cfg else None
                if parent_id_cleanup:
                    try:
                        api.delete_config(
                            component_id=TEST_COMPONENT_ID, config_id=str(parent_id_cleanup)
                        )
                    except Exception as exc:
                        print(
                            f"  [cleanup] Failed to delete "
                            f"{TEST_COMPONENT_ID}/{parent_id_cleanup}: {exc}"
                        )
                if auto_vars_id:
                    try:
                        api.delete_config(component_id="keboola.variables", config_id=auto_vars_id)
                    except Exception as exc:
                        print(
                            f"  [cleanup] Failed to delete keboola.variables/{auto_vars_id}: {exc}"
                        )


# ---------------------------------------------------------------------------
# Tool command tests (requires MCP server)
# ---------------------------------------------------------------------------


@skip_without_credentials
@skip_without_mcp
@pytest.mark.e2e
class TestE2EToolCommands:
    """Test MCP tool list and call commands."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        """Register a project for tool tests."""
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-tool"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_tool_list(self) -> None:
        """tool list should return a list of available MCP tools."""
        result = self._run("tool", "list", "--project", self.alias)
        assert result.exit_code == 0

    def test_tool_call_get_buckets(self) -> None:
        """tool call get_buckets should return bucket data."""
        result = self._run(
            "tool",
            "call",
            "get_buckets",
            "--project",
            self.alias,
        )
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Job run variable values resolution (PR2 / P0-2)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EJobRunVariableValues:
    """Prove `kbagent job run` auto-resolves variableValuesId against a live API.

    Sets up a real `keboola.variables` config with one row and a parent
    ex-http config whose `configuration.variables_id` points at it,
    then runs `kbagent --json job run --no-wait` and asserts the
    response's `resolvedVariableValuesId` matches the created row id.

    Also spot-checks the client path directly via `JobService.resolve_variable_values_id`
    (pure resolver, no Queue dispatch) so a Queue outage would not mask a
    resolver regression.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-jobvars"

        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

        # Client for fixture setup / teardown.
        self.client = KeboolaClient(stack_url=self.url, token=self.token)

        # Track created configs so teardown can delete them even on assert fail.
        self._created: list[tuple[str, str]] = []

        yield

        for component_id, config_id in reversed(self._created):
            try:
                self.client.delete_config(component_id=component_id, config_id=config_id)
            except Exception as exc:
                print(
                    f"  {_DIM}(teardown) delete_config {component_id}/{config_id} failed: {exc}{_RESET}"
                )
        self.client.close()

    def _create_fixture(self) -> tuple[str, str, str]:
        """Create variables config + row + linked parent ex-http config.

        Returns ``(variables_config_id, variables_row_id, parent_config_id)``.
        """
        vars_cfg = self.client.create_config(
            component_id="keboola.variables",
            name=f"{RUN_ID}-vars",
            description="E2E PR2 fixture",
            configuration={
                "variables": [{"name": "year_start", "type": "string"}],
            },
        )
        vars_cfg_id = str(vars_cfg["id"])
        self._created.append(("keboola.variables", vars_cfg_id))

        vars_row = self.client.create_config_row(
            component_id="keboola.variables",
            config_id=vars_cfg_id,
            name="default",
            configuration={"values": [{"name": "year_start", "value": "2016"}]},
        )
        vars_row_id = str(vars_row["id"])

        parent_cfg = self.client.create_config(
            component_id="keboola.ex-http",
            name=f"{RUN_ID}-http-linked",
            description="E2E PR2 linked parent",
            configuration={
                "parameters": {"baseUrl": "https://example.com"},
                "variables_id": vars_cfg_id,
            },
        )
        parent_cfg_id = str(parent_cfg["id"])
        self._created.append(("keboola.ex-http", parent_cfg_id))

        return vars_cfg_id, vars_row_id, parent_cfg_id

    def test_resolve_variable_values_id_live(self) -> None:
        """Resolver reads configuration.variables_id + falls back to first row."""
        from keboola_agent_cli.services.job_service import JobService

        _step(1, "create variables + linked parent fixture")
        _vars_id, vars_row_id, parent_id = self._create_fixture()

        _step(2, "resolve values row id via JobService")
        resolved = JobService.resolve_variable_values_id(
            client=self.client,
            component_id="keboola.ex-http",
            config_id=parent_id,
        )
        print(f"  {_DIM}resolved={resolved} expected={vars_row_id}{_RESET}")
        assert resolved == vars_row_id

    def test_job_run_surfaces_resolved_variable_values_id(self) -> None:
        """`kbagent job run --no-wait` returns resolvedVariableValuesId in --json.

        The job itself may fail at execution time (test token may not have
        rights to run HTTP jobs or the URL may be unreachable). That is OK:
        what we assert is that the resolver picked up the values row and
        kbagent surfaced it before/with job submission.
        """
        _step(1, "create variables + linked parent fixture")
        _vars_id, vars_row_id, parent_id = self._create_fixture()

        _step(2, "kbagent --json job run (no --wait)")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                parent_id,
            ],
        )

        data = _json(result)
        payload = data.get("data", data)
        print(f"  {_DIM}resolvedVariableValuesId={payload.get('resolvedVariableValuesId')}{_RESET}")
        assert payload.get("resolvedVariableValuesId") == vars_row_id

        # Clean up the job we just created (avoid wasted compute) if the
        # Queue accepted it. Best-effort: ignore "not killable" transitions.
        import contextlib

        job_id = payload.get("id")
        if job_id:
            with contextlib.suppress(Exception):
                self.client.kill_job(str(job_id))

    def test_job_run_explicit_override_wins_over_resolver(self) -> None:
        """`--variable-values-id ROW_ID` bypasses the resolver and lands in the job.

        Creates a fixture with TWO values rows (default + alt). Without
        --variable-values-id, the resolver picks the first row. With
        --variable-values-id set to the SECOND row's id, the service must
        use the user's choice, and we assert `resolvedVariableValuesId`
        (really: echoed-back) matches the override, not the first row.
        """
        import contextlib

        _step(1, "create variables + 2 values rows + linked parent")
        vars_cfg_id, default_row_id, parent_id = self._create_fixture()

        # Add a second row and use its id as the override.
        alt_row = self.client.create_config_row(
            component_id="keboola.variables",
            config_id=vars_cfg_id,
            name="alt",
            configuration={"values": [{"name": "year_start", "value": "2020"}]},
        )
        alt_row_id = str(alt_row["id"])
        assert alt_row_id != default_row_id

        _step(2, "kbagent job run --variable-values-id <alt>")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                parent_id,
                "--variable-values-id",
                alt_row_id,
            ],
        )

        data = _json(result)
        payload = data.get("data", data)
        assert payload.get("resolvedVariableValuesId") == alt_row_id
        job_id = payload.get("id")
        if job_id:
            with contextlib.suppress(Exception):
                self.client.kill_job(str(job_id))

    def test_job_run_no_variables_skips_resolution(self) -> None:
        """`--no-variables` suppresses the resolver; no `resolvedVariableValuesId` surfaces.

        Locks the opt-out contract: a component that happens to have a
        linked variables config can still be run without variable binding
        when the caller explicitly asks (e.g. manual debug runs).
        """
        import contextlib

        _step(1, "create variables + linked parent fixture")
        _vars_id, _row_id, parent_id = self._create_fixture()

        _step(2, "kbagent job run --no-variables")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                parent_id,
                "--no-variables",
            ],
        )

        data = _json(result)
        payload = data.get("data", data)
        # Key omitted entirely when resolution was skipped.
        assert "resolvedVariableValuesId" not in payload
        job_id = payload.get("id")
        if job_id:
            with contextlib.suppress(Exception):
                self.client.kill_job(str(job_id))

    def test_job_run_no_variable_rows_surfaces_error_code(self) -> None:
        """Linked variables config with zero rows exits with `NO_VARIABLE_ROWS`.

        Agent-facing contract: when a transformation is hooked up to a
        variables config that has not yet had any row created, kbagent
        must fail fast rather than submitting a job that will silently
        bind empty strings at runtime.
        """
        _step(1, "create empty variables config + linked parent (no rows)")
        # Variables config WITHOUT any row.
        vars_cfg = self.client.create_config(
            component_id="keboola.variables",
            name=f"{RUN_ID}-empty-vars",
            description="E2E PR2: empty values",
            configuration={"variables": [{"name": "year_start", "type": "string"}]},
        )
        vars_cfg_id = str(vars_cfg["id"])
        self._created.append(("keboola.variables", vars_cfg_id))

        parent_cfg = self.client.create_config(
            component_id="keboola.ex-http",
            name=f"{RUN_ID}-http-empty-link",
            description="E2E PR2: parent with empty-variables link",
            configuration={
                "parameters": {"baseUrl": "https://example.com"},
                "variables_id": vars_cfg_id,
            },
        )
        parent_id = str(parent_cfg["id"])
        self._created.append(("keboola.ex-http", parent_id))

        _step(2, "kbagent job run -> expect NO_VARIABLE_ROWS")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                parent_id,
            ],
        )

        assert result.exit_code != 0
        try:
            data = json.loads(result.output)
        except json.JSONDecodeError:
            pytest.fail(f"Expected JSON error output, got: {result.output}")
        assert data.get("status") == "error"
        assert data.get("error", {}).get("code") == "NO_VARIABLE_ROWS"

    def test_resolver_prefers_explicit_values_id_over_first_row(self) -> None:
        """`configuration.variables_values_id` wins over first-row fallback.

        Directly tests the resolver short-circuit path without touching the
        Queue API. If a config has pinned a specific values row via the
        Keboola UI or sync push, kbagent must honor that selection even
        when additional rows exist.
        """
        from keboola_agent_cli.services.job_service import JobService

        _step(1, "create variables + 2 rows; parent pins the SECOND row")
        vars_cfg_id, first_row_id, _ = self._create_fixture()

        alt_row = self.client.create_config_row(
            component_id="keboola.variables",
            config_id=vars_cfg_id,
            name="pinned",
            configuration={"values": [{"name": "year_start", "value": "2025"}]},
        )
        pinned_row_id = str(alt_row["id"])
        assert pinned_row_id != first_row_id

        # Patch the parent to point at the pinned row explicitly.
        pinned_parent = self.client.create_config(
            component_id="keboola.ex-http",
            name=f"{RUN_ID}-http-pinned",
            description="E2E PR2: parent pinned to specific values row",
            configuration={
                "parameters": {"baseUrl": "https://example.com"},
                "variables_id": vars_cfg_id,
                "variables_values_id": pinned_row_id,
            },
        )
        pinned_parent_id = str(pinned_parent["id"])
        self._created.append(("keboola.ex-http", pinned_parent_id))

        _step(2, "resolver returns the pinned row, NOT the first row")
        resolved = JobService.resolve_variable_values_id(
            client=self.client,
            component_id="keboola.ex-http",
            config_id=pinned_parent_id,
        )
        print(f"  {_DIM}resolved={resolved} pinned={pinned_row_id} first={first_row_id}{_RESET}")
        assert resolved == pinned_row_id


# ---------------------------------------------------------------------------
# PR8: Config metadata + Workspace GC (standalone, no storage dependency)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EPR8ConfigMetadata:
    """End-to-end tests for config metadata CRUD commands (PR8).

    Creates a real keboola.ex-db-snowflake config, exercises the full
    metadata round-trip (metadata-list / set-metadata / get-metadata /
    delete-metadata / set-folder), then deletes the config.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-meta"

        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        self.api = KeboolaClient(self.url, self.token)
        self._created_config_ids: list[tuple[str, str]] = []

        # Register project
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        yield
        for comp_id, cfg_id in self._created_config_ids:
            with contextlib.suppress(Exception):
                self.api.delete_config(comp_id, cfg_id)

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_config_metadata_crud_roundtrip(self) -> None:
        """Full metadata CRUD: list (empty) → set → get → list (present) → delete → list (gone)."""
        # Create a config to attach metadata to
        cfg = self.api.create_config(
            component_id=TEST_COMPONENT_ID,
            name=f"{RUN_ID}-meta-test",
            configuration={},
            description="E2E PR8 metadata test",
        )
        config_id = str(cfg["id"])
        self._created_config_ids.append((TEST_COMPONENT_ID, config_id))

        custom_key = f"E2E.PR8.{RUN_ID}"

        _step(1, "metadata-list on fresh config -- should be empty")
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        assert isinstance(data["data"]["metadata"], list)
        initial_count = len(data["data"]["metadata"])

        _step(2, "set-metadata -- upsert custom key")
        data = self._run_ok(
            "config",
            "set-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--key",
            custom_key,
            "--value",
            "pr8-value",
        )
        assert data["data"]["key"] == custom_key
        assert data["data"]["value"] == "pr8-value"

        _step(3, "get-metadata -- value round-trips")
        data = self._run_ok(
            "config",
            "get-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--key",
            custom_key,
        )
        assert data["data"]["value"] == "pr8-value"

        _step(4, "metadata-list -- custom key appears")
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        entries = data["data"]["metadata"]
        assert len(entries) == initial_count + 1
        match = next((e for e in entries if e.get("key") == custom_key), None)
        assert match is not None
        metadata_id = str(match["id"])

        _step(5, "delete-metadata -- remove by ID")
        data = self._run_ok(
            "config",
            "delete-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--metadata-id",
            metadata_id,
            "--yes",
        )
        assert metadata_id in data["data"]["message"]

        _step(6, "metadata-list after delete -- key is gone")
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        remaining = {e.get("key") for e in data["data"]["metadata"]}
        assert custom_key not in remaining

    def test_set_folder_sugar(self) -> None:
        """set-folder writes KBC.configuration.folderName metadata."""
        cfg = self.api.create_config(
            component_id=TEST_COMPONENT_ID,
            name=f"{RUN_ID}-folder-test",
            configuration={},
            description="E2E PR8 set-folder test",
        )
        config_id = str(cfg["id"])
        self._created_config_ids.append((TEST_COMPONENT_ID, config_id))

        folder_name = f"PR8-Folder-{RUN_ID}"

        _step(1, "set-folder -- write KBC.configuration.folderName")
        data = self._run_ok(
            "config",
            "set-folder",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--name",
            folder_name,
        )
        assert data["data"]["folder"] == folder_name
        assert data["data"]["key"] == "KBC.configuration.folderName"

        _step(2, "metadata-list -- folder key is visible")
        data = self._run_ok(
            "config",
            "metadata-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        folder_entry = next(
            (e for e in data["data"]["metadata"] if e.get("key") == "KBC.configuration.folderName"),
            None,
        )
        assert folder_entry is not None
        assert folder_entry["value"] == folder_name

    def test_get_metadata_missing_key_exits_1(self) -> None:
        """get-metadata for a non-existent key returns exit code 1."""
        cfg = self.api.create_config(
            component_id=TEST_COMPONENT_ID,
            name=f"{RUN_ID}-meta-missing",
            configuration={},
            description="E2E PR8 missing key test",
        )
        config_id = str(cfg["id"])
        self._created_config_ids.append((TEST_COMPONENT_ID, config_id))

        result = self._run(
            "config",
            "get-metadata",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--key",
            "does.not.exist",
        )
        assert result.exit_code == 1
        data = json.loads(result.output)
        assert data["status"] == "error"


@skip_without_credentials
@pytest.mark.e2e
class TestE2EPR8WorkspaceGC:
    """End-to-end tests for workspace list --orphaned and workspace gc (PR8).

    Creates a real workspace, deletes its backing sandbox config via direct API
    call to manufacture an orphan, then verifies the GC commands detect and
    remove it.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-gc"

        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        self.api = KeboolaClient(self.url, self.token)
        self._created_workspace_ids: list[int] = []

        # Register project
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "project",
                "add",
                "--project",
                self.alias,
                "--url",
                self.url,
                "--token",
                self.token,
            ],
        )
        assert result.exit_code == 0, f"project add failed: {result.output}"

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        yield
        for ws_id in self._created_workspace_ids:
            with contextlib.suppress(Exception):
                self.api.delete_workspace(ws_id)

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_workspace_gc_orphan_roundtrip(self) -> None:
        """Create workspace, orphan it by deleting sandbox config, verify GC finds and removes it."""
        _step(1, "workspace create")
        result = self._run("workspace", "create", "--project", self.alias)
        if result.exit_code != 0:
            pytest.skip(f"workspace create not supported: {result.output}")

        data = _json_ok(result)
        ws_id = data["data"]["workspace_id"]
        assert ws_id > 0
        self._created_workspace_ids.append(ws_id)

        _step(2, "retrieve workspace to find sandbox config_id")
        ws_data = self.api.get_workspace(ws_id)
        config_id = str(ws_data.get("configurationId") or ws_data.get("config_id") or "")
        if not config_id:
            pytest.skip("workspace has no configurationId, cannot manufacture orphan")

        _step(3, "delete sandbox config to make the workspace orphaned")
        try:
            self.api.delete_config("keboola.sandboxes", config_id)
        except Exception as exc:
            pytest.skip(f"could not delete sandbox config: {exc}")

        _step(4, "workspace list --orphaned -- workspace should appear")
        data = self._run_ok("workspace", "list", "--project", self.alias, "--orphaned")
        orphan_ids = [w["id"] for w in data["data"]["workspaces"]]
        assert ws_id in orphan_ids, f"ws {ws_id} not listed as orphan; got: {orphan_ids}"

        _step(5, "workspace gc --dry-run -- counts but does not delete")
        data = self._run_ok("workspace", "gc", "--project", self.alias, "--dry-run")
        gc_data = data["data"]
        assert gc_data["dry_run"] is True
        would_delete_ids = [w["id"] for w in gc_data.get("would_delete", [])]
        assert ws_id in would_delete_ids

        # Verify workspace still exists after dry-run
        remaining = self._run_ok("workspace", "list", "--project", self.alias, "--orphaned")
        assert ws_id in [w["id"] for w in remaining["data"]["workspaces"]]

        _step(6, "workspace gc --yes -- deletes the orphan")
        data = self._run_ok("workspace", "gc", "--project", self.alias, "--yes")
        gc_data = data["data"]
        assert gc_data["dry_run"] is False
        deleted_ids = [w["id"] for w in gc_data.get("deleted", [])]
        assert ws_id in deleted_ids

        # Remove from cleanup tracker since GC deleted it
        if ws_id in self._created_workspace_ids:
            self._created_workspace_ids.remove(ws_id)

        _step(7, "workspace list --orphaned -- workspace is gone")
        data = self._run_ok("workspace", "list", "--project", self.alias, "--orphaned")
        remaining_ids = [w["id"] for w in data["data"]["workspaces"]]
        assert ws_id not in remaining_ids
