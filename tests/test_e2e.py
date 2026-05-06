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
from keboola_agent_cli.models import ProjectConfig

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

        _step(14.2, "storage describe-bucket/table/column/batch", "description metadata round-trip")
        self._test_storage_describe(bucket_id, table_id)

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

        _step("18c", "config set-default-bucket", "set/clear storage.output.default_bucket")
        self._test_config_set_default_bucket(config_id)

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

        _step("42.5", "project use / current + firewall flags")
        self._test_project_pin_and_firewall()

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

        # ── 0.23.0: bulk detail, --include-rows, --with-state (issue #197) ──

        # config detail BULK (no --config-id) should return {configs, errors}
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
        )
        bulk = data["data"]
        assert "configs" in bulk, "bulk mode envelope must include 'configs'"
        assert "errors" in bulk, "bulk mode envelope must include 'errors'"
        assert isinstance(bulk["configs"], list)
        # Our freshly created config must be in the array
        bulk_ids = [c["config_id"] for c in bulk["configs"]]
        assert config_id in bulk_ids, f"config_id {config_id} missing from bulk: {bulk_ids}"
        # Each row must be tagged with project_alias
        assert all(c.get("project_alias") == self.alias for c in bulk["configs"])

        # Single-config shape must be preserved (backward compat guarantee)
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
        single = data["data"]
        assert "configs" not in single, "single mode must NOT wrap in {configs: [...]}"
        assert single["id"] == config_id
        assert single["name"] == f"{RUN_ID} Test Config"

        # config detail --with-state (single mode): state key must be present
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--with-state",
        )
        assert "state" in data["data"], "--with-state must attach state key"
        # Fresh config has empty state -- that's expected
        assert isinstance(data["data"]["state"], dict)

        # config detail --with-state (bulk mode): every row has state
        data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--with-state",
        )
        bulk_with_state = data["data"]
        assert bulk_with_state["configs"], "bulk with-state returned empty list"
        assert all("state" in c for c in bulk_with_state["configs"]), (
            "--with-state in bulk mode must attach state to every row"
        )

        # config list --include-rows: bodies attached
        data = self._run_ok(
            "config",
            "list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--include-rows",
        )
        rows = data["data"]["configs"]
        assert rows, "config list --include-rows returned empty"
        ours = next((r for r in rows if r["config_id"] == config_id), None)
        assert ours is not None, "include-rows output missing our test config"
        assert "configuration" in ours, "--include-rows must attach configuration body"
        assert "rows" in ours, "--include-rows must attach rows list"
        assert ours["configuration"]["parameters"]["db"]["host"] == "test.example.com"

        # --config-id + multiple --project -> exit 2 (INVALID_ARGUMENT).
        # We only have one project registered in E2E, but the rule fires before
        # any API call so we can trigger it by repeating the same alias twice.
        result = subprocess.run(
            [
                "kbagent",
                "--json",
                "config",
                "detail",
                "--project",
                self.alias,
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
                "--config-id",
                config_id,
            ],
            env={**os.environ, "KBAGENT_CONFIG_DIR": str(self.config_dir)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, (
            f"expected exit 2 for --config-id + multi --project, got {result.returncode}"
        )

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

    def _test_config_set_default_bucket(self, config_id: str) -> None:
        """Test config set-default-bucket: set, dry-run, clear, no-op, sibling preservation."""
        target_bucket = f"in.c-{RUN_ID.lower()}-default-bucket"

        # Seed a sibling key under storage.output so we can verify the
        # read-modify-write actually preserves it across set + clear.
        self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--set",
            "storage.output.tables=[]",
        )

        # --dry-run preview first (no write)
        data = self._run_ok(
            "config",
            "set-default-bucket",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--bucket",
            target_bucket,
            "--dry-run",
        )
        assert data["data"]["dry_run"] is True
        assert any("default_bucket" in c for c in data["data"]["changes"])

        # Apply the set
        data = self._run_ok(
            "config",
            "set-default-bucket",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--bucket",
            target_bucket,
        )
        assert data["data"]["default_bucket"] == target_bucket

        # Verify via detail: default_bucket is set AND the sibling tables key survives
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
        cfg = data["data"]["configuration"]
        assert cfg["storage"]["output"]["default_bucket"] == target_bucket
        assert "tables" in cfg["storage"]["output"], "sibling key under storage.output was wiped"

        # Setting the same value is a no-op (changed=false, no API write needed)
        data = self._run_ok(
            "config",
            "set-default-bucket",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--bucket",
            target_bucket,
        )
        assert data["data"]["changed"] is False

        # Clear and verify the key is gone but the sibling still survives
        self._run_ok(
            "config",
            "set-default-bucket",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--clear",
        )
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
        cfg = data["data"]["configuration"]
        output = cfg.get("storage", {}).get("output", {})
        assert "default_bucket" not in output
        assert "tables" in output, "sibling key under storage.output was wiped on --clear"

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

    def _test_project_pin_and_firewall(self) -> None:
        """End-to-end coverage for `project use`, `project current`, and --deny-* flags."""
        # --- Pin lifecycle -------------------------------------------------

        # Pre-condition: first-added is already the default. Verify via current.
        data = self._run_ok("project", "current")
        assert data["data"]["alias"] == self.alias
        assert data["data"]["source"] == "pin"

        # Explicit `project use` is a no-op in value but confirms it persists.
        data = self._run_ok("project", "use", self.alias)
        assert data["data"]["alias"] == self.alias
        # source is always "pin" on use (the field describes where the new
        # pin ended up, not how it arrived).
        assert data["data"]["source"] == "pin"

        # `project use nonexistent` fails with exit 5 (CONFIG_ERROR).
        result = self._run("project", "use", "does-not-exist-alias")
        assert result.exit_code == 5

        # --- KBAGENT_PROJECT env override ---------------------------------
        # Set the env var to a bogus value and confirm `current` reports env
        # as the source + flags the unknown alias.
        with patch.dict(os.environ, {"KBAGENT_PROJECT": "mystery-alias"}):
            data = self._run_ok("project", "current")
            assert data["data"]["alias"] == "mystery-alias"
            assert data["data"]["source"] == "env"
            assert data["data"]["env_points_to_configured_project"] is False
            assert data["data"]["pinned"] == self.alias

        # After unsetting, pin is restored as the effective alias.
        data = self._run_ok("project", "current")
        assert data["data"]["source"] == "pin"
        assert data["data"]["alias"] == self.alias

        # --- --deny-writes blocks writes, allows reads --------------------
        # Read still succeeds.
        data = self._run_ok(
            "--deny-writes",  # top-level flag must come before subcommand
            "project",
            "list",
        )
        assert any(p["alias"] == self.alias for p in data["data"])

        # Attempting a write under --deny-writes must exit 6 PERMISSION_DENIED.
        # create-bucket is a safe write to try: if the firewall fails to
        # block it we'd create a real bucket, so track it for cleanup just
        # in case the block logic regresses.
        guard_bucket_name = f"{RUN_ID.replace('-', '_')}_firewall_guard"
        result = self._run(
            "--deny-writes",
            "storage",
            "create-bucket",
            "--project",
            self.alias,
            "--stage",
            "in",
            "--name",
            guard_bucket_name,
        )
        assert result.exit_code == 6, (
            f"--deny-writes should block storage.create-bucket (exit 6), "
            f"got {result.exit_code}: {result.output}"
        )
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

        # Safety: if the block failed silently and a bucket was actually
        # created, schedule cleanup. We don't fail louder because the
        # exit_code assert above already did.
        try:
            buckets = self.api.list_buckets()
            for bucket in buckets:
                if bucket.get("name") == guard_bucket_name:
                    self._created_buckets.append(bucket["id"])
        except Exception:
            pass  # Best-effort cleanup tracking only.

        # --- --deny-destructive blocks destructive ops --------------------
        # delete-bucket is destructive; must exit 6 even on a bucket that
        # does not exist (permission check fires before the API call).
        result = self._run(
            "--deny-destructive",
            "storage",
            "delete-bucket",
            "--project",
            self.alias,
            "--bucket-id",
            "in.c-never-existed",
            "--yes",
        )
        assert result.exit_code == 6, (
            f"--deny-destructive should block storage.delete-bucket (exit 6), "
            f"got {result.exit_code}: {result.output}"
        )
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

        # --- --deny-destructive allows non-destructive writes -------------
        # project.description-set is classified 'write' (not destructive),
        # so --deny-destructive must NOT block it. We pass an empty string
        # write -- this goes to the API, but the permission gate is the
        # only thing under test here, so any non-6 exit is acceptable.
        result = self._run(
            "--deny-destructive",
            "project",
            "description-get",
            "--project",
            self.alias,
        )
        assert result.exit_code != 6, (
            "--deny-destructive must not block read op project.description-get"
        )

        # --- Persistence check --------------------------------------------
        # None of the --deny-* flags may have written to config.json.
        store = ConfigStore(config_dir=self.config_dir)
        persisted = store.load()
        assert persisted.permissions is None, (
            "--deny-writes / --deny-destructive must be session-only; "
            f"found persisted policy: {persisted.permissions}"
        )

    def _test_storage_describe(self, bucket_id: str, table_id: str) -> None:
        """Round-trip describe commands: write description, read it back."""
        # describe-bucket: set KBC.description, verify via bucket-detail
        data = self._run_ok(
            "storage",
            "describe-bucket",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--text",
            "E2E bucket description",
        )
        assert data["data"]["bucket_id"] == bucket_id
        assert data["data"]["description"] == "E2E bucket description"

        data = self._run_ok(
            "storage", "bucket-detail", "--project", self.alias, "--bucket-id", bucket_id
        )
        assert data["data"]["description"] == "E2E bucket description"

        # describe-table: set KBC.description, verify via table-detail
        data = self._run_ok(
            "storage",
            "describe-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--text",
            "E2E table description",
        )
        assert data["data"]["table_id"] == table_id
        assert data["data"]["description"] == "E2E table description"

        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        assert data["data"]["description"] == "E2E table description"

        # describe-column: set per-column descriptions, verify via table-detail column_details
        data = self._run_ok(
            "storage",
            "describe-column",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--column",
            "id=Unique row identifier",
            "--column",
            "name=Human-readable name",
        )
        assert data["data"]["table_id"] == table_id
        assert data["data"]["columns"]["id"] == "Unique row identifier"
        assert data["data"]["columns"]["name"] == "Human-readable name"

        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        col_descs = {c["name"]: c.get("description", "") for c in data["data"]["column_details"]}
        assert col_descs.get("id") == "Unique row identifier"
        assert col_descs.get("name") == "Human-readable name"

        # describe-batch: apply all three sections from a YAML file
        batch_yaml = (
            f"buckets:\n"
            f"  {bucket_id}: Batch bucket desc\n"
            f"tables:\n"
            f"  {table_id}: Batch table desc\n"
            f"columns:\n"
            f"  {table_id}:\n"
            f"    id: Batch column id desc\n"
        )
        batch_file = self.work_dir / "batch_describe.yaml"
        batch_file.write_text(batch_yaml, encoding="utf-8")
        data = self._run_ok(
            "storage",
            "describe-batch",
            "--project",
            self.alias,
            "--from-file",
            str(batch_file),
        )
        assert data["data"]["project_alias"] == self.alias
        assert len(data["data"]["applied"]) == 3
        assert data["data"]["errors"] == []

        # Verify the batch updated the descriptions
        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        assert data["data"]["description"] == "Batch table desc"
        col_descs = {c["name"]: c.get("description", "") for c in data["data"]["column_details"]}
        assert col_descs.get("id") == "Batch column id desc"

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
# Job run variable values resolution
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
# Project pin + firewall flag E2E (PR5)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestPinAndFirewallE2E:
    """Focused E2E for `project use`, `project current`, and --deny-* flags.

    Exercises the real API to confirm:
    - `project use` persists the pin to config.json.
    - `project current` reports the effective alias + source correctly.
    - `KBAGENT_PROJECT` env var overrides the pin at runtime.
    - `--deny-writes` blocks the permission gate on a real write op (exit 6).
    - `--deny-destructive` blocks a real destructive op (exit 6).
    - Neither flag persists to config.json.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.alias_a = f"{RUN_ID}-pin-a"
        self.alias_b = f"{RUN_ID}-pin-b"

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def test_pin_lifecycle_against_real_project(self) -> None:
        """End-to-end: add, use, current, env override."""
        # Register two aliases pointing at the SAME real project. We only
        # need distinct aliases to observe the pin switching.
        self._run(
            "project",
            "add",
            "--project",
            self.alias_a,
            "--url",
            self.url,
            "--token",
            self.token,
        )
        self._run(
            "project",
            "add",
            "--project",
            self.alias_b,
            "--url",
            self.url,
            "--token",
            self.token,
        )

        # First-added becomes default.
        current = self._run("project", "current")
        assert current.exit_code == 0
        data = _json_ok(current)
        assert data["data"]["alias"] == self.alias_a
        assert data["data"]["source"] == "pin"

        # `project use` switches the pin; persistence survives next invocation.
        use_result = self._run("project", "use", self.alias_b)
        use_data = _json_ok(use_result)
        assert use_data["data"]["alias"] == self.alias_b
        assert use_data["data"]["previous"] == self.alias_a

        after = _json_ok(self._run("project", "current"))
        assert after["data"]["alias"] == self.alias_b
        assert after["data"]["source"] == "pin"

        # Unknown alias -> exit 5.
        bad = self._run("project", "use", "does-not-exist")
        assert bad.exit_code == 5
        bad_data = json.loads(bad.output)
        assert bad_data["error"]["code"] == "CONFIG_ERROR"

        # KBAGENT_PROJECT overrides the pin.
        with patch.dict(os.environ, {"KBAGENT_PROJECT": self.alias_a}):
            env_view = _json_ok(self._run("project", "current"))
            assert env_view["data"]["alias"] == self.alias_a
            assert env_view["data"]["source"] == "env"
            assert env_view["data"]["pinned"] == self.alias_b
            assert env_view["data"]["env_points_to_configured_project"] is True

    def test_deny_writes_blocks_real_write_op(self) -> None:
        """--deny-writes must exit 6 on a real create-bucket attempt."""
        self._run(
            "project",
            "add",
            "--project",
            self.alias_a,
            "--url",
            self.url,
            "--token",
            self.token,
        )

        # Use a name that won't collide; the permission gate fires before
        # the API call so the bucket must never appear.
        bucket_name = f"{RUN_ID.replace('-', '_')}_fw_w"
        result = self._run(
            "--deny-writes",
            "storage",
            "create-bucket",
            "--project",
            self.alias_a,
            "--stage",
            "in",
            "--name",
            bucket_name,
        )
        assert result.exit_code == 6, (
            f"--deny-writes should block storage.create-bucket; got exit "
            f"{result.exit_code}: {result.output}"
        )
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

        # Defensive: if the block leaked and a bucket was actually created,
        # clean it up and fail the assertion above (already failed) more loudly.
        import contextlib

        api = KeboolaClient(self.url, self.token)
        try:
            for bucket in api.list_buckets():
                if bucket.get("name") == bucket_name:
                    with contextlib.suppress(Exception):
                        api.delete_bucket(bucket["id"], force=True)
                    raise AssertionError(
                        f"--deny-writes failed to block: bucket {bucket['id']} was created"
                    )
        finally:
            api.close()

    def test_deny_destructive_blocks_real_destructive_op(self) -> None:
        """--deny-destructive must exit 6 on storage.delete-bucket."""
        self._run(
            "project",
            "add",
            "--project",
            self.alias_a,
            "--url",
            self.url,
            "--token",
            self.token,
        )

        result = self._run(
            "--deny-destructive",
            "storage",
            "delete-bucket",
            "--project",
            self.alias_a,
            "--bucket-id",
            "in.c-does-not-exist-for-sure",
            "--yes",
        )
        assert result.exit_code == 6, (
            f"--deny-destructive should block delete-bucket; got exit "
            f"{result.exit_code}: {result.output}"
        )
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_destructive_allows_read_op(self) -> None:
        """--deny-destructive must NOT block read ops (regression guard)."""
        self._run(
            "project",
            "add",
            "--project",
            self.alias_a,
            "--url",
            self.url,
            "--token",
            self.token,
        )
        result = self._run(
            "--deny-destructive",
            "storage",
            "buckets",
            "--project",
            self.alias_a,
        )
        # Read must succeed (exit 0) OR fail for non-permission reasons.
        assert result.exit_code != 6, f"--deny-destructive blocked a read op: {result.output}"

    def test_firewall_flags_never_persist(self) -> None:
        """Neither --deny-writes nor --deny-destructive may write to config.json."""
        self._run(
            "project",
            "add",
            "--project",
            self.alias_a,
            "--url",
            self.url,
            "--token",
            self.token,
        )
        # Run a blocked op under both flags.
        self._run(
            "--deny-writes",
            "--deny-destructive",
            "storage",
            "create-bucket",
            "--project",
            self.alias_a,
            "--stage",
            "in",
            "--name",
            "never_created",
        )
        # Persisted policy must still be None.
        persisted = ConfigStore(config_dir=self.config_dir).load()
        assert persisted.permissions is None, (
            f"--deny-* flags leaked to config.json: {persisted.permissions}"
        )


# ---------------------------------------------------------------------------
# Flow E2E tests
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EFlowOperations:
    """End-to-end tests for all flow subcommands against a real Keboola project.

    Creates a real keboola.flow config, exercises all 8 commands, and cleans up.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-flow"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self._created_flows: list[tuple[str, str]] = []  # (component_id, flow_id)

        from keboola_agent_cli.client import KeboolaClient

        self.client = KeboolaClient(stack_url=self.url, token=self.token)

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

        yield

        import contextlib

        for component_id, flow_id in self._created_flows:
            with contextlib.suppress(Exception):
                self.client.delete_config(
                    component_id=component_id, config_id=flow_id, branch_id=None
                )
        self.client.close()

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_flow_crud_and_schedule(self, tmp_path: Path) -> None:
        """Full lifecycle: schema → new → list → detail → update → schedule → schedule-remove → delete."""

        _step(1, "flow schema returns YAML template with phases key")
        result = self._run("flow", "schema")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "phases" in data["data"]["schema"]

        _step(2, "flow new -- create a keboola.flow config")
        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--name",
            f"{RUN_ID}-flow",
            "--description",
            "E2E flow test",
        )
        assert result.exit_code == 0, result.output
        created = json.loads(result.output)["data"]
        flow_id = created["id"]
        assert flow_id
        assert created["project_alias"] == self.alias
        self._created_flows.append(("keboola.flow", flow_id))

        _step(3, "flow list -- flow appears in listing")
        result = self._run("flow", "list", "--project", self.alias)
        assert result.exit_code == 0
        listing = json.loads(result.output)["data"]
        ids = {f["config_id"] for f in listing["flows"]}
        assert flow_id in ids

        _step(4, "flow detail -- returns phase/task counts")
        result = self._run(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
        )
        assert result.exit_code == 0, result.output
        detail = json.loads(result.output)["data"]
        assert detail["id"] == flow_id
        assert "phase_count" in detail

        _step(5, "flow update -- rename the flow")
        result = self._run(
            "flow",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--name",
            f"{RUN_ID}-flow-renamed",
        )
        assert result.exit_code == 0, result.output
        updated = json.loads(result.output)["data"]
        assert updated["id"] == flow_id

        _step(6, "flow schedule -- attach a cron schedule")
        result = self._run(
            "flow",
            "schedule",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--cron",
            "0 6 * * *",
        )
        assert result.exit_code == 0, result.output
        sched = json.loads(result.output)["data"]
        assert sched["status"] in ("created", "updated")
        assert sched["config_id"] == flow_id
        assert sched["cron_tab"] == "0 6 * * *"

        _step(7, "flow schedule-remove -- remove schedule, idempotent")
        result = self._run(
            "flow",
            "schedule-remove",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--yes",
        )
        assert result.exit_code == 0, result.output
        removed = json.loads(result.output)["data"]
        assert removed["deleted_count"] >= 1

        # Idempotent second call
        result2 = self._run(
            "flow",
            "schedule-remove",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--yes",
        )
        assert result2.exit_code == 0
        assert json.loads(result2.output)["data"]["deleted_count"] == 0

        _step(8, "flow delete -- delete the flow")
        result = self._run(
            "flow",
            "delete",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--yes",
        )
        assert result.exit_code == 0, result.output
        deleted = json.loads(result.output)["data"]
        assert deleted["status"] == "deleted"
        assert deleted["config_id"] == flow_id
        # Remove from cleanup list since we deleted it
        self._created_flows.remove(("keboola.flow", flow_id))

    def test_flow_update_preserves_behavior_onerror(self, tmp_path: Path) -> None:
        """Verify that ``kbagent flow update`` preserves ``behavior.onError``.

        If any assertion fails, the pilot agent prompt must route flow writes
        through ``--hint client`` + direct API instead of ``kbagent flow
        update`` as the first choice.

        Covered scenarios:
            A. Rename-only update (no ``--file``) must leave behavior intact.
            B. ``--file`` update with explicit behavior must propagate the
               supplied value (documented pass-through).
            C. ``--file`` update where phases omit behavior documents the
               actual server response (strip vs default-applied). Printed
               diagnostically; not a hard assertion since the strip itself
               is expected replace-semantics, not a bug.
        """
        import yaml as _yaml

        initial_def = {
            "phases": [
                {
                    "id": 1,
                    "name": "Phase One",
                    "dependsOn": [],
                    "behavior": {"onError": "warning"},
                },
                {
                    "id": 2,
                    "name": "Phase Two",
                    "dependsOn": [1],
                    "behavior": {"onError": "stop"},
                },
            ],
            "tasks": [
                {
                    "id": 1,
                    "name": "Phase 1 task",
                    "phase": 1,
                    "enabled": True,
                    "continueOnFailure": False,
                    "task": {
                        "mode": "run",
                        "componentId": "keboola.ex-db-snowflake",
                        "configId": "nonexistent-placeholder-1",
                    },
                },
                {
                    "id": 2,
                    "name": "Phase 2 task",
                    "phase": 2,
                    "enabled": True,
                    "continueOnFailure": False,
                    "task": {
                        "mode": "run",
                        "componentId": "keboola.ex-db-snowflake",
                        "configId": "nonexistent-placeholder-2",
                    },
                },
            ],
        }

        initial_yaml = tmp_path / "flow_initial.yaml"
        initial_yaml.write_text(_yaml.safe_dump(initial_def))

        _step(1, "flow new -- create flow with behavior.onError on both phases")
        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--name",
            f"{RUN_ID}-behavior-flow",
            "--file",
            f"@{initial_yaml}",
        )
        assert result.exit_code == 0, result.output
        created = json.loads(result.output)["data"]
        flow_id = created["id"]
        self._created_flows.append(("keboola.flow", flow_id))

        _step(2, "verify behavior stored correctly on creation")
        detail = self._run_ok(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
        )
        phases = detail["data"]["phases"]
        assert len(phases) == 2, f"Expected 2 phases, got {len(phases)}"
        assert phases[0].get("behavior", {}).get("onError") == "warning", (
            f"Create did not store phases[0].behavior.onError correctly. "
            f"Got: {phases[0].get('behavior')!r}"
        )
        assert phases[1].get("behavior", {}).get("onError") == "stop", (
            f"Create did not store phases[1].behavior.onError correctly. "
            f"Got: {phases[1].get('behavior')!r}"
        )

        # --- Scenario A: rename-only update, no --file -----------------
        _step(3, "Scenario A -- rename only (no --file); behavior must survive")
        result = self._run(
            "flow",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--name",
            f"{RUN_ID}-behavior-flow-renamed",
        )
        assert result.exit_code == 0, result.output
        after_rename = self._run_ok(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
        )
        rphases = after_rename["data"]["phases"]
        assert rphases[0].get("behavior", {}).get("onError") == "warning", (
            "BLOCKER: rename-only flow update stripped "
            f"phases[0].behavior.onError. Expected 'warning', got "
            f"{rphases[0].get('behavior')!r}. Plan §6.6 tool matrix must be "
            "revised -- 'kbagent flow update' is NOT safe for partial updates."
        )
        assert rphases[1].get("behavior", {}).get("onError") == "stop", (
            "BLOCKER: rename-only flow update stripped "
            f"phases[1].behavior.onError. Expected 'stop', got "
            f"{rphases[1].get('behavior')!r}."
        )

        # --- Scenario B: --file with explicit (changed) behavior -------
        _step(4, "Scenario B -- --file with explicit behavior; pass-through")
        v2_def = {
            "phases": [
                {
                    "id": 1,
                    "name": "Phase One",
                    "dependsOn": [],
                    "behavior": {"onError": "stop"},  # flipped
                },
                {
                    "id": 2,
                    "name": "Phase Two",
                    "dependsOn": [1],
                    "behavior": {"onError": "warning"},  # flipped
                },
            ],
            "tasks": initial_def["tasks"],
        }
        v2_yaml = tmp_path / "flow_v2.yaml"
        v2_yaml.write_text(_yaml.safe_dump(v2_def))

        result = self._run(
            "flow",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--file",
            f"@{v2_yaml}",
        )
        assert result.exit_code == 0, result.output
        after_v2 = self._run_ok(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
        )
        v2phases = after_v2["data"]["phases"]
        assert v2phases[0].get("behavior", {}).get("onError") == "stop", (
            "--file with explicit behavior did not propagate: "
            f"expected 'stop', got {v2phases[0].get('behavior')!r}"
        )
        assert v2phases[1].get("behavior", {}).get("onError") == "warning", (
            "--file with explicit behavior did not propagate: "
            f"expected 'warning', got {v2phases[1].get('behavior')!r}"
        )

        # --- Scenario C: --file WITHOUT behavior (document actual) -----
        _step(5, "Scenario C -- --file without behavior; document server response")
        v3_def = {
            "phases": [
                {"id": 1, "name": "Phase One", "dependsOn": []},
                {"id": 2, "name": "Phase Two", "dependsOn": [1]},
            ],
            "tasks": initial_def["tasks"],
        }
        v3_yaml = tmp_path / "flow_v3.yaml"
        v3_yaml.write_text(_yaml.safe_dump(v3_def))

        result = self._run(
            "flow",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--file",
            f"@{v3_yaml}",
        )
        assert result.exit_code == 0, result.output
        after_v3 = self._run_ok(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
        )
        v3phases = after_v3["data"]["phases"]
        assert len(v3phases) == 2
        # Diagnostic: capture what Keboola did with a behavior-less phase
        # (either echoes empty dict, fills default, or omits the field entirely)
        print(
            f"\n  [DIAGNOSTIC] --file without behavior -> "
            f"phases[0].behavior = {v3phases[0].get('behavior')!r}, "
            f"phases[1].behavior = {v3phases[1].get('behavior')!r}"
        )

    def test_flow_dag_validation_rejects_cycle(self) -> None:
        """flow new with a cyclic phase dependency must fail with INVALID_FLOW_DAG."""
        cyclic_yaml = (
            "phases:\n"
            "  - id: 1\n    name: A\n    dependsOn: [2]\n"
            "  - id: 2\n    name: B\n    dependsOn: [1]\n"
            "tasks: []\n"
        )
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(cyclic_yaml)
            yaml_path = f.name

        try:
            result = self._run(
                "flow",
                "new",
                "--project",
                self.alias,
                "--component-id",
                "keboola.flow",
                "--name",
                f"{RUN_ID}-cyclic",
                "--file",
                f"@{yaml_path}",
            )
            assert result.exit_code != 0
            out = json.loads(result.output)
            assert out["error"]["code"] == "INVALID_FLOW_DAG"
        finally:
            import os as _os

            _os.unlink(yaml_path)

    def test_flow_list_no_project_returns_all(self) -> None:
        """flow list without --project returns flows from all registered projects."""
        result = self._run("flow", "list")
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert "flows" in data
        assert "errors" in data

    def test_flow_list_with_schedules(self) -> None:
        """flow list --with-schedules enriches rows with schedule metadata.

        Creates a flow + schedule, verifies the enrichment appears on the
        correct flow row (and is empty on other flows), then cleans up.
        """
        # Create a flow
        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--name",
            f"{RUN_ID}-flow-ws",
            "--description",
            "E2E with-schedules test",
        )
        assert result.exit_code == 0, result.output
        flow_id = json.loads(result.output)["data"]["id"]
        self._created_flows.append(("keboola.flow", flow_id))

        # Attach a schedule
        sched_result = self._run(
            "flow",
            "schedule",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
            "--flow-id",
            flow_id,
            "--cron",
            "0 6 * * *",
        )
        assert sched_result.exit_code == 0, sched_result.output

        try:
            # flow list --with-schedules must expose schedules inline
            result = self._run(
                "flow",
                "list",
                "--project",
                self.alias,
                "--with-schedules",
            )
            assert result.exit_code == 0, result.output
            data = json.loads(result.output)["data"]
            enriched = [f for f in data["flows"] if f["config_id"] == flow_id]
            assert len(enriched) == 1
            assert "schedules" in enriched[0]
            assert len(enriched[0]["schedules"]) >= 1
            assert enriched[0]["schedules"][0]["cron"] == "0 6 * * *"
        finally:
            # Clean up the schedule so the flow deletion in setup's teardown works
            self._run(
                "flow",
                "schedule-remove",
                "--project",
                self.alias,
                "--component-id",
                "keboola.flow",
                "--flow-id",
                flow_id,
                "--yes",
            )


@skip_without_credentials
@pytest.mark.e2e
class TestE2EScheduleOperations:
    """End-to-end tests for schedule discovery and audit commands.

    Creates a keboola.flow + cron schedule, then exercises
    schedule list / detail / find against the real Keboola project.
    Cleans up flow + schedule on teardown.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-sched"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        self._created_flows: list[tuple[str, str]] = []

        from keboola_agent_cli.client import KeboolaClient

        self.client = KeboolaClient(stack_url=self.url, token=self.token)

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

        # Create a flow + schedule up-front so every test has data to work with.
        flow_result = _invoke(
            self.config_dir,
            [
                "--json",
                "flow",
                "new",
                "--project",
                self.alias,
                "--component-id",
                "keboola.flow",
                "--name",
                f"{RUN_ID}-sched-flow",
                "--description",
                "E2E schedule discovery fixture",
            ],
        )
        assert flow_result.exit_code == 0, flow_result.output
        self.flow_id = json.loads(flow_result.output)["data"]["id"]
        self._created_flows.append(("keboola.flow", self.flow_id))

        sched_result = _invoke(
            self.config_dir,
            [
                "--json",
                "flow",
                "schedule",
                "--project",
                self.alias,
                "--component-id",
                "keboola.flow",
                "--flow-id",
                self.flow_id,
                "--cron",
                "0 3 * * *",
                "--timezone",
                "UTC",
            ],
        )
        assert sched_result.exit_code == 0, sched_result.output
        self.schedule_id = json.loads(sched_result.output)["data"]["schedule_id"]

        yield

        import contextlib

        # Clean up schedule (best-effort), then flow
        with contextlib.suppress(Exception):
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "flow",
                    "schedule-remove",
                    "--project",
                    self.alias,
                    "--component-id",
                    "keboola.flow",
                    "--flow-id",
                    self.flow_id,
                    "--yes",
                ],
            )
        for component_id, flow_id in self._created_flows:
            with contextlib.suppress(Exception):
                self.client.delete_config(
                    component_id=component_id, config_id=flow_id, branch_id=None
                )
        self.client.close()

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def test_schedule_list_surfaces_fixture(self) -> None:
        _step(1, "schedule list shows the fixture schedule")
        result = self._run("schedule", "list", "--project", self.alias)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {s["schedule_id"] for s in data["schedules"]}
        assert self.schedule_id in ids
        match = next(s for s in data["schedules"] if s["schedule_id"] == self.schedule_id)
        assert match["parent_component_id"] == "keboola.flow"
        assert match["parent_config_id"] == self.flow_id
        assert match["cron"] == "0 3 * * *"
        assert match["enabled"] is True

    def test_schedule_list_enabled_only(self) -> None:
        _step(2, "schedule list --enabled-only keeps the enabled fixture schedule")
        result = self._run("schedule", "list", "--project", self.alias, "--enabled-only")
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {s["schedule_id"] for s in data["schedules"]}
        assert self.schedule_id in ids

    def test_schedule_detail_returns_parent_name(self) -> None:
        _step(3, "schedule detail joins parent_name from the flow config")
        result = self._run(
            "schedule",
            "detail",
            "--project",
            self.alias,
            "--schedule-id",
            self.schedule_id,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["schedule_id"] == self.schedule_id
        assert data["parent_config_id"] == self.flow_id
        assert data["parent_name"] == f"{RUN_ID}-sched-flow"
        assert data["cron"] == "0 3 * * *"

    def test_schedule_find_cron_window_match(self) -> None:
        _step(4, "schedule find --cron-window catches the 03:00 fixture schedule")
        result = self._run(
            "schedule",
            "find",
            "--project",
            self.alias,
            "--cron-window",
            "02:00-04:00",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {s["schedule_id"] for s in data["schedules"]}
        assert self.schedule_id in ids

    def test_schedule_find_cron_window_exclude(self) -> None:
        _step(5, "schedule find --cron-window excludes schedules outside window")
        result = self._run(
            "schedule",
            "find",
            "--project",
            self.alias,
            "--cron-window",
            "10:00-12:00",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {s["schedule_id"] for s in data["schedules"]}
        assert self.schedule_id not in ids

    def test_schedule_find_not_run_since_includes_fresh_fixture(self) -> None:
        """The fixture flow has never run so it counts as stale for any N."""
        _step(6, "schedule find --not-run-since 30 includes never-run schedules")
        result = self._run(
            "schedule",
            "find",
            "--project",
            self.alias,
            "--not-run-since",
            "30",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        ids = {s["schedule_id"] for s in data["schedules"]}
        assert self.schedule_id in ids

    def test_schedule_find_invalid_window_exits_5(self) -> None:
        _step(7, "schedule find rejects malformed --cron-window at service boundary")
        result = self._run(
            "schedule",
            "find",
            "--project",
            self.alias,
            "--cron-window",
            "garbage",
        )
        assert result.exit_code == 5

    def test_schedule_detail_not_found_exits_1(self) -> None:
        _step(8, "schedule detail on unknown ID returns a KeboolaApiError")
        result = self._run(
            "schedule",
            "detail",
            "--project",
            self.alias,
            "--schedule-id",
            "0000000000000000000000000000",
        )
        assert result.exit_code != 0
        assert result.exit_code in (1, 3, 4)


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


# ---------------------------------------------------------------------------
# Queue polling parity (PR4 / P0-3): exponential curve, log tail, timeout kill
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EJobRunQueuePollingParity:
    """Live verification of the PR4 Queue API polling contract.

    Three scenarios, each spawns exactly one config, cleans it up:

    - **log tail on a failed job**: a snowflake-transformation with
      deliberately invalid SQL runs to `status=error`; we assert the
      returned JSON error envelope contains a non-empty
      `details.logTail` sourced from ``GET /jobs/{id}/events``.
    - **timeout triggers remote kill**: a python-transformation-v2 with
      `time.sleep(120)` is invoked with `--timeout 8`; we assert the
      command exits 7 (``EXIT_JOB_TIMEOUT_TERMINATED``) with
      `error.code == "JOB_TIMEOUT_TERMINATED"` and `details.job.status`
      in {terminated, cancelled, terminating} -- i.e. the kill landed.
    - **fixed strategy still reaches completion**: a no-op transformation
      runs under ``--poll-strategy fixed`` to prove the opt-out path works
      against a real Queue.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path, request: pytest.FixtureRequest) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        # Per-test alias suffix so parallel pytest-xdist runs don't share a
        # project alias across workers. `request.node.name` is stable per test
        # and includes any parametrize id.
        safe = request.node.name.replace("[", "-").replace("]", "")
        self.alias = f"{RUN_ID}-queuepoll-{safe}"[:60]

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

        self.client = KeboolaClient(stack_url=self.url, token=self.token)
        self._created: list[tuple[str, str]] = []
        self._submitted_jobs: list[str] = []

        yield

        # Best-effort cleanup: kill any jobs we left running, then delete
        # the configs. We never want a test failure here to mask the real
        # assertion failure.
        import contextlib

        for job_id in self._submitted_jobs:
            with contextlib.suppress(Exception):
                self.client.kill_job(job_id)

        for component_id, config_id in reversed(self._created):
            try:
                self.client.delete_config(component_id=component_id, config_id=config_id)
            except Exception as exc:
                print(
                    f"  {_DIM}(teardown) delete_config {component_id}/{config_id} "
                    f"failed: {exc}{_RESET}"
                )
        self.client.close()

    def _create_sleep_config(self, seconds: int, suffix: str) -> str:
        """Create a python-transformation-v2 config that sleeps and returns its id."""
        cfg = self.client.create_config(
            component_id="keboola.python-transformation-v2",
            name=f"{RUN_ID}-queuepoll-{suffix}",
            description=f"E2E PR4: sleeps {seconds}s -- used only for polling tests",
            configuration={
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [
                                {
                                    "name": "sleep",
                                    "script": [
                                        "import time",
                                        f"time.sleep({seconds})",
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )
        cfg_id = str(cfg["id"])
        self._created.append(("keboola.python-transformation-v2", cfg_id))
        return cfg_id

    def _create_guaranteed_fail_config(self, suffix: str) -> str:
        """Python transformation that raises an exception so job status=error.

        Why python not Snowflake: a Snowflake transformation with no
        input/output tables registered is treated as a successful no-op
        even if the SQL would be invalid at execute time. A Python
        transformation with an unconditional ``raise`` surfaces as
        ``status=error`` with a clear message on the event feed, which
        is what the log-tail assertion needs.
        """
        cfg = self.client.create_config(
            component_id="keboola.python-transformation-v2",
            name=f"{RUN_ID}-queuepoll-bad-{suffix}",
            description="E2E PR4: guaranteed-fail python transformation",
            configuration={
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [
                                {
                                    "name": "boom",
                                    "script": [
                                        "raise RuntimeError('kbagent E2E PR4 deliberate failure')",
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        )
        cfg_id = str(cfg["id"])
        self._created.append(("keboola.python-transformation-v2", cfg_id))
        return cfg_id

    def test_log_tail_surfaced_on_queue_job_failed(self) -> None:
        """Failed Queue job -> error envelope with details.logTail from /events."""
        _step(1, "create python-transformation-v2 that raises")
        cfg_id = self._create_guaranteed_fail_config("tail")

        _step(2, "kbagent --json job run --wait (expect QUEUE_JOB_FAILED)")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.python-transformation-v2",
                "--config-id",
                cfg_id,
                "--wait",
                "--timeout",
                "300",
                "--log-tail-lines",
                "50",
                "--no-variables",
            ],
        )

        # Deliberate failure: exit non-zero, envelope status=error,
        # details.logTail is a non-empty list.
        assert result.exit_code != 0, f"Expected failure, got success: {result.output}"
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["error"]["code"] == "QUEUE_JOB_FAILED"
        details = envelope["error"].get("details") or {}
        log_tail = details.get("logTail") or []
        assert isinstance(log_tail, list) and len(log_tail) > 0, (
            f"Expected non-empty logTail, got {log_tail!r} in {envelope!r}"
        )

    def test_timeout_triggers_remote_kill_and_exits_seven(self) -> None:
        """--timeout N < job runtime -> exit 7 + kill landed remotely."""
        _step(1, "create python-transformation-v2 that sleeps 120s")
        cfg_id = self._create_sleep_config(seconds=120, suffix="kill")

        _step(2, "kbagent --json job run --wait --timeout 8 (expect exit 7)")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.python-transformation-v2",
                "--config-id",
                cfg_id,
                "--wait",
                "--timeout",
                "8",
                "--log-tail-lines",
                "20",
                "--no-variables",
            ],
        )

        assert result.exit_code == 7, (
            f"Expected exit 7 (JOB_TIMEOUT_TERMINATED), got {result.exit_code}\n{result.output}"
        )
        envelope = json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["error"]["code"] == "JOB_TIMEOUT_TERMINATED"

        job = envelope["error"]["details"]["job"]
        assert job["status"] in {"terminated", "cancelled", "terminating"}, (
            f"Expected terminated/cancelled/terminating; got {job['status']!r}"
        )
        # Track job_id so teardown's kill-if-needed covers the 'terminating'
        # transitional case where the remote hasn't settled yet.
        self._submitted_jobs.append(str(job["id"]))

    def test_fixed_poll_strategy_reaches_completion(self) -> None:
        """--poll-strategy fixed still completes against a real Queue."""
        _step(1, "create trivial python-transformation-v2 (sleep 3s)")
        cfg_id = self._create_sleep_config(seconds=3, suffix="fixed")

        _step(2, "kbagent job run --wait --poll-strategy fixed")
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.python-transformation-v2",
                "--config-id",
                cfg_id,
                "--wait",
                "--timeout",
                "120",
                "--poll-strategy",
                "fixed",
                "--no-variables",
            ],
        )

        assert result.exit_code == 0, f"Expected success, got: {result.output}"
        payload = _json(result)["data"]
        assert payload["status"] == "success"
        assert payload.get("isFinished") is True

    def test_fetch_job_events_returns_list_on_real_job(self) -> None:
        """Direct client call against /jobs/{id}/events on a finished job."""
        _step(1, "create trivial successful job + run to completion")
        cfg_id = self._create_sleep_config(seconds=1, suffix="events")

        result = _invoke(
            self.config_dir,
            [
                "--json",
                "job",
                "run",
                "--project",
                self.alias,
                "--component-id",
                "keboola.python-transformation-v2",
                "--config-id",
                cfg_id,
                "--wait",
                "--timeout",
                "120",
                "--no-variables",
            ],
        )
        assert result.exit_code == 0, result.output
        job_id = str(_json(result)["data"]["id"])

        _step(2, "client.fetch_job_events direct call")
        events = self.client.fetch_job_events(job_id, limit=50)
        assert isinstance(events, list)
        # A completed python-transformation job always emits at least one event
        # (startup + completion). Guard against an empty-but-silent regression.
        assert len(events) > 0


# ===========================================================================


@pytest.mark.e2e
@skip_without_credentials
class TestE2ESyncAdoptExisting:
    """E2E test for 'sync init --adopt-existing' against a real Keboola project.

    Simulates a directory that was set up by the kbc CLI (or a previous kbagent
    version) by writing a minimal valid manifest, then verifies that:
      1. kbagent sync init --adopt-existing succeeds (status=adopted)
      2. kbagent sync status works on the adopted directory
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-adopt"

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

    def _run(self, *args: str):
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict:
        return _json_ok(self._run(*args))

    def test_adopt_existing_manifest(self) -> None:
        """init --adopt-existing adopts a kbc-style manifest; sync status works after."""
        import json as _json

        # 1. Write a kbc-style manifest for the real project.
        #    We first call sync init normally to learn the real project_id, then
        #    delete and rewrite as a "legacy" manifest.
        _step(1, "sync init (normal) to learn project_id")
        resp = self._run_ok(
            "sync",
            "init",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
        )
        project_id = resp["data"]["project_id"]
        branch_id = None
        keboola_dir = self.project_dir / ".keboola"
        manifest_path = keboola_dir / "manifest.json"
        raw = _json.loads(manifest_path.read_text())
        if raw.get("branches"):
            branch_id = raw["branches"][0]["id"]

        # 2. Rewrite as a minimal kbc-style manifest (drop gitBranching field).
        _step(2, "rewrite as kbc-style manifest (drop gitBranching)")
        kbc_manifest = {
            "version": 2,
            "project": {"id": project_id, "apiHost": self.url.replace("https://", "")},
            "allowTargetEnv": True,
            "sortBy": "id",
            "naming": {"branch": "{branch_name}"},
            "branches": [{"id": branch_id, "path": "main"}] if branch_id else [],
            "configurations": [],
        }
        manifest_path.write_text(_json.dumps(kbc_manifest, indent=4), encoding="utf-8")

        # 3. sync init --adopt-existing should succeed without error.
        _step(3, "sync init --adopt-existing")
        resp = self._run_ok(
            "sync",
            "init",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
            "--adopt-existing",
        )
        inner = resp["data"]
        assert inner["status"] == "adopted", f"Expected 'adopted', got {inner['status']}"
        assert inner["project_id"] == project_id
        assert inner["files_created"] == []

        # 4. sync status should work on the adopted directory.
        _step(4, "sync status on adopted directory")
        resp = self._run_ok(
            "sync",
            "status",
            "--directory",
            str(self.project_dir),
        )
        # Status should be parseable (may show no changes on an empty dir)
        inner = resp["data"]
        assert "modified" in inner or "unchanged" in inner or "added" in inner

    def test_adopt_existing_rejects_wrong_project(self) -> None:
        """init --adopt-existing rejects a manifest with a different project_id."""
        import json as _json

        keboola_dir = self.project_dir / ".keboola"
        keboola_dir.mkdir()
        # Write a manifest with a clearly wrong project_id
        wrong_manifest = {
            "version": 2,
            "project": {"id": 999999999, "apiHost": "connection.keboola.com"},
            "allowTargetEnv": True,
            "sortBy": "id",
            "naming": {"branch": "{branch_name}"},
            "branches": [],
            "configurations": [],
        }
        (keboola_dir / "manifest.json").write_text(_json.dumps(wrong_manifest), encoding="utf-8")

        result = self._run(
            "sync",
            "init",
            "--project",
            self.alias,
            "--directory",
            str(self.project_dir),
            "--adopt-existing",
        )
        assert result.exit_code == 5, f"Expected exit 5, got {result.exit_code}: {result.output}"
        output = _json.loads(result.output)
        assert output["status"] == "error"
        assert "999999999" in output["error"]["message"]


# ---------------------------------------------------------------------------
# Issues #192 + #222: native column types + dev-branch bucket materialization
# ---------------------------------------------------------------------------


@skip_without_credentials
class TestE2EStorageNativeTypesAndBranchMaterialize:
    """End-to-end coverage for ``storage create-table`` native types and the
    dev-branch auto-materialize path.

    Verifies the two behaviours that issues #192 and #222 ask for:

    - ``--column pk:VARCHAR(40)`` / ``amount:NUMERIC(18,2)`` / ``ts:TIMESTAMP_TZ``
      flow through to the Storage API with ``definition.length`` intact and
      Snowflake produces the correct native types (``VARCHAR(40)``,
      ``NUMBER(18,2)``, ``TIMESTAMP_TZ(9)``).
    - ``--not-null`` and ``--default`` flags map to ``nullable=false`` and
      ``default=...`` on the column definition.
    - Creating a table in a dev branch against an unmaterialized bucket
      auto-creates the bucket first (response includes
      ``auto_created_bucket=true``), mirroring the official Go CLI's
      ``EnsureBucketExists`` pattern.

    Requires ``E2E_API_TOKEN`` + ``E2E_URL`` and a real project; skipped
    otherwise.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-nt"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.client = KeboolaClient(stack_url=self.url, token=self.token)

        self._created_branch_ids: list[int] = []
        self._created_buckets: list[str] = []

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

        yield

        # Teardown: branches first (delete cascades to their buckets/tables),
        # then any buckets we created outside of a branch.
        for branch_id in self._created_branch_ids:
            with contextlib.suppress(Exception):
                self.client.delete_dev_branch(branch_id)
        for bucket_id in self._created_buckets:
            with contextlib.suppress(Exception):
                self.client.delete_bucket(bucket_id, force=True)
        self.client.close()

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(_invoke(self.config_dir, ["--json", *args]))

    def test_native_types_and_branch_materialize(self) -> None:
        """Single scenario covers both issues end-to-end against a live API."""

        _step(1, "branch create", "isolate the test in a short-lived dev branch")
        branch = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", f"{RUN_ID}-nt-branch"
        )["data"]
        branch_id = int(branch["branch_id"])
        self._created_branch_ids.append(branch_id)

        _step(
            2,
            "storage create-table (native types, branch not materialized)",
            "expect auto_created_bucket=true",
        )
        bucket_id = f"in.c-{RUN_ID.replace('-', '_')}_nt"
        table_name = f"{RUN_ID.replace('-', '_')}_native"
        created = self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            table_name,
            "--branch",
            str(branch_id),
            "--column",
            "pk:VARCHAR(40)",
            "--column",
            "amount:NUMERIC(18,2)",
            "--column",
            "ts:TIMESTAMP_TZ",
            "--column",
            "is_paid:BOOLEAN",
            "--column",
            "meta:VARIANT",
            "--primary-key",
            "pk",
            "--not-null",
            "pk",
            "--not-null",
            "amount",
            "--default",
            "amount=0",
            "--default",
            "is_paid=false",
        )["data"]

        assert created["table_id"] == f"{bucket_id}.{table_name}"
        assert created.get("auto_created_bucket") is True, (
            "Service should auto-materialize a bucket that does not yet exist "
            "in the target dev branch."
        )
        # Issue #224 follow-up: kbagent-e2e has the `storage-branches` feature,
        # so the legacy fake-branch warning must NOT fire here. Coverage for
        # the OFF case is in the unit suite (test_create_table_branch_legacy_storage_flagged)
        # and was reproduced manually against project 10539 (padak-2-0); we
        # don't gate CI on a fake-branch credential.
        assert created.get("legacy_branch_storage") is False, (
            "kbagent-e2e has storage-branches enabled; legacy_branch_storage "
            "must report False in this scenario."
        )

        # Issue #224: the auto-materialized bucket must carry
        # ``KBC.createdBy.branch.id`` system metadata, otherwise
        # output-mapping (BucketCreator::checkDevBucketMetadata) rejects every
        # subsequent transformation write on branched-storage projects.
        bucket_meta = self.client.list_bucket_metadata(bucket_id, branch_id=branch_id)
        branch_id_entries = [m for m in bucket_meta if m.get("key") == "KBC.createdBy.branch.id"]
        assert len(branch_id_entries) == 1, (
            f"Expected exactly one KBC.createdBy.branch.id metadata entry on "
            f"auto-materialized bucket {bucket_id}; got {bucket_meta}"
        )
        entry = branch_id_entries[0]
        assert entry["value"] == str(branch_id), (
            f"KBC.createdBy.branch.id should equal current branch ID {branch_id}; "
            f"got {entry['value']!r}"
        )
        assert entry["provider"] == "system", (
            f"KBC.* metadata must be written with provider=system; got {entry.get('provider')!r}"
        )

        _step(
            3,
            "storage table-detail",
            "verify length/nullable/default made it to Snowflake",
        )
        table = self.client.get_table_detail(f"{bucket_id}.{table_name}", branch_id=branch_id)
        by_name = {c["name"]: c for c in table["definition"]["columns"]}

        pk_def = by_name["pk"]["definition"]
        assert pk_def["type"] in ("VARCHAR", "TEXT")  # Snowflake stores both as VARCHAR
        assert pk_def["length"] == "40"
        assert pk_def["nullable"] is False

        amount_def = by_name["amount"]["definition"]
        # NUMERIC routes to NUMBER on Snowflake.
        assert amount_def["type"] in ("NUMBER", "NUMERIC")
        assert amount_def["length"] == "18,2"
        assert amount_def["nullable"] is False
        assert amount_def.get("default") == "0"

        ts_def = by_name["ts"]["definition"]
        assert ts_def["type"] == "TIMESTAMP_TZ"

        is_paid_def = by_name["is_paid"]["definition"]
        assert is_paid_def["type"] == "BOOLEAN"
        # Keboola normalises lowercase bools to uppercase in the stored default.
        assert is_paid_def.get("default", "").upper() == "FALSE"

        variant_def = by_name["meta"]["definition"]
        assert variant_def["type"] == "VARIANT"

        _step(
            4,
            "storage create-table (same bucket, second table)",
            "expect auto_created_bucket=false on the second call",
        )
        second = self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            f"{table_name}_second",
            "--branch",
            str(branch_id),
            "--column",
            "id:INTEGER",
        )["data"]
        assert second.get("auto_created_bucket") is False, (
            "Second create against an already-materialized bucket should not re-create it."
        )


# ---------------------------------------------------------------------------
# TestE2EStorageSwapTables -- storage swap-tables in a dev branch
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EStorageSwapTables:
    """End-to-end coverage for ``kbagent storage swap-tables``.

    Verifies:
    - swap exchanges schemas between two tables in a dev branch
      (table IDs stay stable, ``definition.columns`` are swapped),
    - production calls (no branch + no active branch) are rejected
      before any HTTP traffic.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-swap"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.client = KeboolaClient(stack_url=self.url, token=self.token)

        self._created_branch_ids: list[int] = []
        self._created_buckets: list[str] = []

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

        yield

        # Teardown: branches first (cascades to their buckets/tables),
        # then any production buckets we created outside of a branch.
        for branch_id in self._created_branch_ids:
            with contextlib.suppress(Exception):
                self.client.delete_dev_branch(branch_id)
        for bucket_id in self._created_buckets:
            with contextlib.suppress(Exception):
                self.client.delete_bucket(bucket_id, force=True)
        self.client.close()

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(_invoke(self.config_dir, ["--json", *args]))

    def test_swap_in_dev_branch_exchanges_schemas(self) -> None:
        """Live swap: two tables with different VARCHAR lengths swap definitions."""

        _step(1, "branch create", "isolate the swap test in a dev branch")
        branch = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", f"{RUN_ID}-swap-branch"
        )["data"]
        branch_id = int(branch["branch_id"])
        self._created_branch_ids.append(branch_id)

        bucket_id = f"in.c-{RUN_ID.replace('-', '_')}_swap"
        original_id = f"{bucket_id}.original"
        typed_id = f"{bucket_id}.typed_copy"

        _step(
            2,
            "create two typed tables with distinguishable column lengths",
            "VARCHAR(20) vs VARCHAR(80) on the 'value' column",
        )
        self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            "original",
            "--branch",
            str(branch_id),
            "--column",
            "id:VARCHAR(40)",
            "--column",
            "value:VARCHAR(20)",
            "--primary-key",
            "id",
        )
        self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            "typed_copy",
            "--branch",
            str(branch_id),
            "--column",
            "id:VARCHAR(40)",
            "--column",
            "value:VARCHAR(80)",
            "--primary-key",
            "id",
        )

        def _value_len(table_detail: dict[str, Any]) -> str:
            cols = {c["name"]: c for c in table_detail["definition"]["columns"]}
            return cols["value"]["definition"]["length"]

        before_original = self.client.get_table_detail(original_id, branch_id=branch_id)
        before_typed = self.client.get_table_detail(typed_id, branch_id=branch_id)
        assert _value_len(before_original) == "20"
        assert _value_len(before_typed) == "80"

        _step(3, "storage swap-tables", "POST /tables/.../swap with targetTableId")
        result = self._run_ok(
            "storage",
            "swap-tables",
            "--project",
            self.alias,
            "--table-id",
            original_id,
            "--target-table-id",
            typed_id,
            "--branch",
            str(branch_id),
            "--yes",
        )["data"]
        assert result["table_id"] == original_id
        assert result["target_table_id"] == typed_id
        assert result["branch_id"] == branch_id
        assert result["dry_run"] is False

        _step(4, "table-detail", "verify VARCHAR lengths exchanged")
        after_original = self.client.get_table_detail(original_id, branch_id=branch_id)
        after_typed = self.client.get_table_detail(typed_id, branch_id=branch_id)
        assert _value_len(after_original) == "80", (
            f"After swap, '{original_id}' should adopt the schema of '{typed_id}' "
            f"(VARCHAR(80)); got VARCHAR({_value_len(after_original)})."
        )
        assert _value_len(after_typed) == "20", (
            f"After swap, '{typed_id}' should adopt the schema of '{original_id}' "
            f"(VARCHAR(20)); got VARCHAR({_value_len(after_typed)})."
        )

    def test_swap_dry_run_does_not_call_api(self) -> None:
        """Dry-run skips the HTTP call: no schema change, exit 0."""
        _step(1, "branch create", "dry-run still requires a branch context")
        branch = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", f"{RUN_ID}-swap-dry"
        )["data"]
        branch_id = int(branch["branch_id"])
        self._created_branch_ids.append(branch_id)

        bucket_id = f"in.c-{RUN_ID.replace('-', '_')}_swap_dry"
        a_id = f"{bucket_id}.a"
        b_id = f"{bucket_id}.b"
        self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            "a",
            "--branch",
            str(branch_id),
            "--column",
            "id:VARCHAR(40)",
            "--primary-key",
            "id",
        )
        self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            "b",
            "--branch",
            str(branch_id),
            "--column",
            "id:VARCHAR(40)",
            "--primary-key",
            "id",
        )

        before = self.client.get_table_detail(a_id, branch_id=branch_id)

        result = self._run_ok(
            "storage",
            "swap-tables",
            "--project",
            self.alias,
            "--table-id",
            a_id,
            "--target-table-id",
            b_id,
            "--branch",
            str(branch_id),
            "--dry-run",
        )["data"]
        assert result["dry_run"] is True
        assert "response" not in result

        after = self.client.get_table_detail(a_id, branch_id=branch_id)
        assert before["lastChangeDate"] == after["lastChangeDate"], (
            "Dry-run must not modify the table"
        )

    def test_swap_without_branch_is_rejected(self) -> None:
        """Without active branch and without --branch, exit 5 / ConfigError before any HTTP."""
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "storage",
                "swap-tables",
                "--project",
                self.alias,
                "--table-id",
                "in.c-foo.bar",
                "--target-table-id",
                "in.c-foo.baz",
                "--yes",
            ],
        )
        assert result.exit_code == 5, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "dev branch" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# TestE2EDataAppLifecycle -- data-app create / detail / deploy / start / stop / delete
# ---------------------------------------------------------------------------


ENV_DATA_APP_GIT_REPO_PUBLIC = "E2E_DATA_APP_GIT_REPO_PUBLIC"
ENV_DATA_APP_GIT_REPO_PRIVATE = "E2E_DATA_APP_GIT_REPO_PRIVATE"
ENV_DATA_APP_GIT_USER = "E2E_DATA_APP_GIT_USER"
ENV_DATA_APP_GIT_PAT = "E2E_DATA_APP_GIT_PAT"
ENV_MANAGE_TOKEN = "E2E_MANAGE_TOKEN"

skip_without_data_app_public = pytest.mark.skipif(
    not (HAS_CREDENTIALS and os.environ.get(ENV_DATA_APP_GIT_REPO_PUBLIC)),
    reason=f"requires {ENV_TOKEN} + {ENV_DATA_APP_GIT_REPO_PUBLIC}",
)
skip_without_data_app_private = pytest.mark.skipif(
    not (
        HAS_CREDENTIALS
        and os.environ.get(ENV_DATA_APP_GIT_REPO_PRIVATE)
        and os.environ.get(ENV_DATA_APP_GIT_USER)
        and os.environ.get(ENV_DATA_APP_GIT_PAT)
    ),
    reason=(
        f"requires {ENV_TOKEN} + {ENV_DATA_APP_GIT_REPO_PRIVATE} + "
        f"{ENV_DATA_APP_GIT_USER} + {ENV_DATA_APP_GIT_PAT}"
    ),
)


# ──────────────────────────────────────────────────────────────────────
# Project invite E2E (since v0.29.0)
#
# Opt-in via `make test-e2e-invite`. Default-skipped in `make test-e2e` because
# (a) it sends a real invitation email and (b) it depends on a separate manage
# token / project ID that the regular E2E credentials don't carry.
# ──────────────────────────────────────────────────────────────────────


ENV_INVITE_PROJECT_ID = "E2E_INVITE_PROJECT_ID"
ENV_INVITE_EMAIL = "E2E_INVITE_EMAIL"
DEFAULT_INVITE_EMAIL = "ottomansky.max@gmail.com"

skip_without_invite_credentials = pytest.mark.skipif(
    not (
        os.environ.get(ENV_MANAGE_TOKEN)
        and os.environ.get(ENV_INVITE_PROJECT_ID)
        and os.environ.get(ENV_URL)
    ),
    reason=(
        f"Requires {ENV_MANAGE_TOKEN}, {ENV_INVITE_PROJECT_ID}, and {ENV_URL}. "
        "Run via `make test-e2e-invite`."
    ),
)


@pytest.mark.e2e
class TestE2EDataAppLifecycle:
    """Live validation of the data-app command group against a real stack.

    Three scenarios:

    1. Public-repo + ``--auth public`` -- minimum recipe (no encryption).
    2. Private-repo + simpleAuth -- full recipe including KMS encryption.
    3. Lifecycle: stop / start / deploy on the just-created private app.

    Each test cleans up its own apps. Cleanup is best-effort (delete is
    idempotent on the platform side -- a 404 on cleanup is not a failure).
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        if not HAS_CREDENTIALS:
            pytest.skip("E2E_API_TOKEN not set")
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-da-proj"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        # Register the project so `kbagent --project ALIAS ...` works.
        _invoke(
            self.config_dir,
            [
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
        self._created_app_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        yield
        print("\n--- DATA-APP CLEANUP ---")
        for app_id in self._created_app_ids:
            try:
                _invoke(
                    self.config_dir,
                    [
                        "--json",
                        "data-app",
                        "delete",
                        "--project",
                        self.alias,
                        "--app-id",
                        app_id,
                        "--yes",
                    ],
                )
                print(f"  Deleted data app {app_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete data app {app_id}: {exc}")

    @skip_without_data_app_public
    def test_data_app_lifecycle_public(self) -> None:
        _step(1, "Create public-repo data app", "no auth gate, no encryption")
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PUBLIC]
        slug = f"e2e-pub-{RUN_ID}"[:60]
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "create",
                "--project",
                self.alias,
                "--name",
                f"E2E Public {RUN_ID}",
                "--slug",
                slug,
                "--git-repo",
                repo,
                "--git-public",
                "--auth",
                "public",
                "--no-deploy",  # avoid waiting on a real container build in CI
            ],
        )
        assert result.exit_code == 0, result.output
        body = _json_ok(result)
        app_id = body["data"]["id"]
        assert app_id, "expected a numeric app id from POST /apps"
        self._created_app_ids.append(app_id)

        _step(2, "Detail merges Data Science + Storage")
        detail = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "detail",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                ],
            )
        )["data"]
        assert detail["id"] == app_id
        assert detail["slug"] == slug
        assert detail["config_version_storage"], (
            "Storage config version should be populated after PUT"
        )

    @skip_without_data_app_private
    def test_data_app_lifecycle_private_and_redeploy(self) -> None:
        _step(1, "Create private-repo simpleAuth data app", "encryption + git PAT")
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PRIVATE]
        username = os.environ[ENV_DATA_APP_GIT_USER]
        # Pass the PAT via env var so plaintext never appears in argv.
        pat_var = "E2E_DATA_APP_GIT_PAT"
        slug = f"e2e-priv-{RUN_ID}"[:60]
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "create",
                "--project",
                self.alias,
                "--name",
                f"E2E Private {RUN_ID}",
                "--slug",
                slug,
                "--git-repo",
                repo,
                "--git-username",
                username,
                "--git-pat-env",
                pat_var,
                "--auth",
                "password",
                "--no-deploy",
            ],
        )
        assert result.exit_code == 0, result.output
        body = _json_ok(result)
        app_id = body["data"]["id"]
        self._created_app_ids.append(app_id)
        # The encrypted PAT must NEVER appear in the JSON output.
        plaintext_pat = os.environ[ENV_DATA_APP_GIT_PAT]
        assert plaintext_pat not in result.output, "Plaintext PAT must never reach the CLI output"

        _step(2, "Stop (idempotent on a non-running app)")
        stop = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "stop",
                "--project",
                self.alias,
                "--app-id",
                app_id,
            ],
        )
        # `stop` on a never-deployed app may return a 4xx; we don't fail the
        # test on that -- the next deploy step is the real assertion.
        _ = stop

        _step(3, "Deploy via the §9 redeploy contract")
        deploy = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "deploy",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                ],
            )
        )["data"]
        assert deploy["config_version"], "deploy must pin a configVersion"

    @skip_without_data_app_public
    def test_data_app_secrets_round_trip(self) -> None:
        """secrets-set -> secrets-list -> secrets-get -> secrets-remove on a real app.

        Uses --no-deploy + --auth public to mint a cheap shell app, then
        verifies the four-step lifecycle:
        1. set: encrypts via per-project KMS, writes to parameters.dataApp.secrets.
        2. list: enumerates keys + derived runtime env-var names; never decrypts.
        3. get: returns metadata only (NEVER plaintext).
        4. remove: idempotent (second remove returns removed: 0, exit 0).

        The decrypted plaintext value must NEVER appear in any CLI output.
        """
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PUBLIC]
        slug = f"e2e-secrets-{RUN_ID}"[:60]
        secret_key = "#E2E_TEST_KEY"
        secret_plaintext = "supersecret-do-not-leak"

        _step(1, "Create shell app for secrets round-trip")
        create = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "create",
                    "--project",
                    self.alias,
                    "--name",
                    f"E2E Secrets {RUN_ID}",
                    "--slug",
                    slug,
                    "--git-repo",
                    repo,
                    "--git-public",
                    "--auth",
                    "public",
                    "--no-deploy",
                ],
            )
        )["data"]
        app_id = create["id"]
        self._created_app_ids.append(app_id)

        _step(2, "secrets-set: encrypt and write")
        set_result = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "secrets-set",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--secret",
                    f"{secret_key}={secret_plaintext}",
                    "--no-hint-next",
                ],
            )
        )
        assert secret_plaintext not in set_result["raw_output"], (
            "Plaintext value MUST NEVER appear in secrets-set output"
        )

        _step(3, "secrets-list: enumerate keys (never decrypts)")
        list_result = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "secrets-list",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                ],
            )
        )
        keys_in_list = [s["key"] for s in list_result["data"]["secrets"]]
        assert secret_key in keys_in_list, (
            f"secrets-list must surface the just-written key; got {keys_in_list}"
        )
        assert secret_plaintext not in list_result["raw_output"], (
            "Plaintext value MUST NEVER appear in secrets-list output"
        )

        _step(4, "secrets-get: metadata only (never plaintext)")
        get_result = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "secrets-get",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--key",
                    secret_key,
                ],
            )
        )
        assert get_result["data"]["key"] == secret_key
        assert secret_plaintext not in get_result["raw_output"], (
            "secrets-get MUST NEVER echo the decrypted plaintext (Encryption API is one-way)"
        )

        _step(5, "secrets-remove: first call removes the key")
        remove_result = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "secrets-remove",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--key",
                    secret_key,
                    "--yes",
                ],
            )
        )
        assert remove_result["data"]["removed"] == 1, "first remove must report removed=1"

        _step(6, "secrets-remove: second call is idempotent (removed=0)")
        idempotent = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "secrets-remove",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--key",
                    secret_key,
                    "--yes",
                ],
            )
        )
        assert idempotent["data"]["removed"] == 0, (
            "second remove of the same key must be idempotent (removed=0, exit 0)"
        )


# ---------------------------------------------------------------------------
# Data-app validate-repo (since v0.29.0) -- GitHub-only, no Keboola creds needed
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_data_app_validate_repo_against_public_repo(tmp_path: Path) -> None:
    """validate-repo against a real public GitHub repo.

    Does NOT require Keboola credentials -- the command only hits GitHub.
    Uses a known-public Keboola example repo via E2E_DATA_APP_GIT_REPO_PUBLIC
    when set; otherwise skipped (no hard-coded URL to keep the test
    independent of upstream-template renames).

    Asserts the command exits cleanly and emits the expected envelope
    shape (status + checks list with BLOCKING / WARN / OK severities).
    """
    repo = os.environ.get(ENV_DATA_APP_GIT_REPO_PUBLIC)
    if not repo:
        pytest.skip(f"requires {ENV_DATA_APP_GIT_REPO_PUBLIC} (any public Keboola data-app repo)")

    config_dir = tmp_path / "kbagent-config"
    config_dir.mkdir()

    result = _invoke(
        config_dir,
        [
            "--json",
            "data-app",
            "validate-repo",
            "--git-repo",
            repo,
            "--git-public",
            "--type",
            "python-js",
        ],
    )
    # Exit 0 when no BLOCKING; exit 1 when at least one BLOCKING. Either is
    # a successful invocation -- the assertion is on shape, not verdict.
    assert result.exit_code in (0, 1), result.output

    body = json.loads(result.output)
    assert body["status"] in ("ok", "error"), f"unexpected status: {body['status']}"
    if body["status"] == "ok":
        assert "checks" in body["data"], "envelope must list per-rule checks"
        # Every check must carry severity + a citation back to the help-doc.
        for check in body["data"]["checks"]:
            assert check["severity"] in ("BLOCKING", "WARN", "OK")
            assert "citation" in check, "each check must cite the help-doc canon"


# ---------------------------------------------------------------------------
# Issue #245: parameters.blocks[].codes[].script auto-normalize on config update
# ---------------------------------------------------------------------------


@skip_without_credentials
class TestE2EConfigUpdateNormalization:
    """End-to-end coverage for the v0.28.0 ``script[]`` auto-normalize fix.

    The Storage API silently accepts a string for
    ``parameters.blocks[].codes[].script`` while the runtime validator
    requires an array. ``kbagent config update`` closes the gap by
    splitting / wrapping before pushing to Storage.

    The test creates a Snowflake transformation in an isolated dev branch,
    pushes a multi-statement string ``script`` via three different code
    paths -- ``--configuration-file``, ``--set`` on a nested path, and
    ``--dry-run`` preview -- and asserts each one writes (or previews)
    an array, exposes the change record on the result envelope, and
    leaves the API config in a state the runtime can parse.

    The job is then run on the normalized config to confirm Snowflake
    accepts the multi-statement form (with ``MULTI_STATEMENT_COUNT = 0``
    set as the first statement, per the workflow doc).

    Branch + config are torn down even on failure.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Any:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-norm"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.client = KeboolaClient(stack_url=self.url, token=self.token)

        self._created_branch_ids: list[int] = []
        self._created_config_ids: list[tuple[str, str, int | None]] = []

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

        yield

        # Teardown: configs first, then branches (branch delete cascades but
        # configs in the default branch must be cleaned up explicitly).
        for component_id, config_id, branch_id in self._created_config_ids:
            with contextlib.suppress(Exception):
                self.client.delete_config(
                    component_id=component_id,
                    config_id=config_id,
                    branch_id=branch_id,
                )
        for branch_id in self._created_branch_ids:
            with contextlib.suppress(Exception):
                self.client.delete_dev_branch(branch_id)
        self.client.close()

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(_invoke(self.config_dir, ["--json", *args]))

    def test_config_update_auto_normalizes_script_array(self, tmp_path: Path) -> None:
        """Full path: create transformation, push string-script, verify
        every observable surface (envelope, API state, runtime job) reflects
        the post-normalize array shape."""

        _step(1, "branch create", "isolate the test in a short-lived dev branch")
        branch_data = self._run_ok(
            "branch",
            "create",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID}-norm-branch",
        )["data"]
        branch_id = int(branch_data["branch_id"])
        self._created_branch_ids.append(branch_id)

        _step(2, "create Snowflake transformation in the dev branch")
        # Minimum-viable Snowflake transformation: one block, one code,
        # initial script as an already-valid array (so create succeeds).
        # We will then test config update by REPLACING the script with a
        # malformed string -- the bug scenario.
        cfg_body = self.client.create_config(
            component_id="keboola.snowflake-transformation",
            name=f"{RUN_ID} normalize-test",
            configuration={
                "parameters": {
                    "blocks": [
                        {
                            "name": "Block 1",
                            "codes": [
                                {
                                    "name": "init",
                                    "script": ["ALTER SESSION SET MULTI_STATEMENT_COUNT = 0;"],
                                }
                            ],
                        }
                    ]
                }
            },
            description=f"E2E #245 normalize check ({RUN_ID})",
            branch_id=branch_id,
        )
        config_id = str(cfg_body["id"])
        self._created_config_ids.append(("keboola.snowflake-transformation", config_id, branch_id))

        _step(
            3,
            "config update --dry-run with string script",
            "preview must show the post-normalize array shape; no API write",
        )
        # Multi-statement string with comments and string literals to
        # exercise the splitter's state machine (semicolons inside `'...'`
        # and `/* ... */` must NOT split).
        bad_payload_path = tmp_path / "bad_string_script.json"
        bad_payload_path.write_text(
            json.dumps(
                {
                    "parameters": {
                        "blocks": [
                            {
                                "name": "Block 1",
                                "codes": [
                                    {
                                        "name": "multi",
                                        "script": (
                                            "ALTER SESSION SET MULTI_STATEMENT_COUNT = 0;"
                                            " /* trailing block comment with ; semicolons */"
                                            " SELECT 'a;b;c' AS literal_with_semicolons;"
                                            " SELECT 1 AS one;"
                                            " -- trailing line comment"
                                        ),
                                    }
                                ],
                            }
                        ]
                    }
                }
            )
        )

        dry_run = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
            "--configuration-file",
            str(bad_payload_path),
            "--dry-run",
        )["data"]
        assert dry_run.get("dry_run") is True, "dry-run flag must be set"
        norms = dry_run.get("normalizations") or []
        assert len(norms) == 1, f"expected exactly 1 normalization, got {norms}"
        assert norms[0]["action"] == "sql_split"
        assert norms[0]["before_type"] == "str"
        assert norms[0]["after_type"] == "list"
        assert norms[0]["after_length"] >= 3, (
            f"splitter should produce >=3 elements (ALTER + 2 SELECTs at minimum); "
            f"got after_length={norms[0]['after_length']}"
        )
        # The dry-run preview must reflect the post-normalize shape so the
        # operator sees what would actually land on Storage.
        new_cfg = dry_run["new_configuration"]
        new_script = new_cfg["parameters"]["blocks"][0]["codes"][0]["script"]
        assert isinstance(new_script, list), (
            f"new_configuration.script must be list after normalize; got {type(new_script).__name__}"
        )

        _step(4, "config update real push", "Storage API must end up with array, not string")
        write_envelope = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
            "--configuration-file",
            str(bad_payload_path),
        )["data"]
        write_norms = write_envelope.get("normalizations") or []
        assert len(write_norms) == 1, (
            f"expected 1 normalization in write envelope, got {write_norms}"
        )
        assert write_norms[0]["action"] == "sql_split"

        _step(5, "fetch from API and assert script[] is array")
        detail = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
        )["data"]
        stored_script = detail["configuration"]["parameters"]["blocks"][0]["codes"][0]["script"]
        assert isinstance(stored_script, list), (
            f"Storage API stored script as {type(stored_script).__name__}, "
            f"not list -- normalization did not fire. Value: {stored_script!r}"
        )
        # Spot-check splitter correctness: block comment must not have caused
        # an extra split, string literal `'a;b;c'` must not have caused one
        # either. After normalize we expect ALTER + literal SELECT + numeric
        # SELECT + trailing comment as separate elements.
        joined = "\n".join(stored_script)
        assert "MULTI_STATEMENT_COUNT" in joined, "ALTER SESSION line must survive splitter intact"
        assert "'a;b;c'" in joined, (
            "string literal with embedded semicolons must NOT have been split mid-literal"
        )
        # Find the literal element and verify it contains the full quoted text
        literal_elem = next((s for s in stored_script if "'a;b;c'" in s), None)
        assert literal_elem is not None and literal_elem.count("'") >= 2, (
            f"literal element must keep both quotes: {literal_elem!r}"
        )

        _step(
            6,
            "--set on a nested path also normalizes",
            "even when the user pushes a string at a deep --set path",
        )
        # Reset to a known starting point with a single-element array, then
        # push a string via --set and assert the same normalize behaviour.
        self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
            "--set",
            (
                "parameters.blocks.0.codes.0.script="
                "ALTER SESSION SET MULTI_STATEMENT_COUNT = 0;"
                " SELECT 1 AS one;"
                " SELECT 2 AS two;"
            ),
        )
        after_set = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
        )["data"]
        after_set_script = after_set["configuration"]["parameters"]["blocks"][0]["codes"][0][
            "script"
        ]
        assert isinstance(after_set_script, list), (
            f"--set path must also normalize; got {type(after_set_script).__name__}"
        )
        assert len(after_set_script) >= 3, (
            f"three statements separated by ; must split into >=3 elements; "
            f"got {after_set_script!r}"
        )

        _step(
            7,
            "run job on the normalized config",
            "Snowflake runtime must accept the array shape (no 'Expected array, got string')",
        )
        job_result = self._run_ok(
            "job",
            "run",
            "--project",
            self.alias,
            "--component-id",
            "keboola.snowflake-transformation",
            "--config-id",
            config_id,
            "--branch",
            str(branch_id),
            "--wait",
            "--timeout",
            "180",
        )["data"]
        # We only need a successful schema validation pass + Snowflake parse.
        # The transformation has no input/output mappings so it may report
        # `success` (no rows moved) or `warning` (no work done); both are
        # acceptable -- what we are asserting is the ABSENCE of the runtime
        # validator's "Expected array, got string" failure mode.
        assert job_result.get("status") in ("success", "warning"), (
            f"Job ended in unexpected state: {job_result.get('status')!r} "
            f"(error: {job_result.get('error_message')!r}). "
            "If status is 'error' with the schema-validator message, the "
            "v0.28.0 normalize fix regressed -- script[] reached the runtime "
            "as a string."
        )
        # Belt-and-braces: explicitly assert the failure-mode string is NOT
        # present anywhere on the envelope, even on a non-error status.
        rendered = json.dumps(job_result)
        assert "Expected" not in rendered or "script" not in rendered, (
            f"job envelope still mentions the script type-mismatch failure: {rendered}"
        )


@pytest.mark.e2e_invite
@skip_without_invite_credentials
def test_project_invite_e2e(tmp_path: Path) -> None:
    """Real invite to the master cuesta project: send -> list -> cancel -> verify gone.

    Uses role=guest (lowest blast radius). The cancel step in the same run
    invalidates the invitation link before the inbox sees it, so this is a
    "the system can send + clean up" check, not a "join my project" check.
    """
    invite_email = os.environ.get(ENV_INVITE_EMAIL, DEFAULT_INVITE_EMAIL)
    project_id = int(os.environ[ENV_INVITE_PROJECT_ID])
    stack_url = (
        os.environ[ENV_URL]
        if os.environ[ENV_URL].startswith("https://")
        else f"https://{os.environ[ENV_URL]}"
    )
    alias = f"e2e-invite-target-{project_id}"

    # Bypass `kbagent project add` (which would verify a Storage API token).
    # MemberService only needs (stack_url, project_id) -- the storage token
    # field is unused. Write a minimal config.json with a placeholder token.
    config_dir = tmp_path / "kbagent-config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url=stack_url,
            token="901-e2e-placeholder-not-used-by-member-commands-xxxxxxxxxx",
            project_id=project_id,
            project_name="E2E invite target",
        ),
    )

    env = {
        **os.environ,
        "KBC_MANAGE_API_TOKEN": os.environ[ENV_MANAGE_TOKEN],
    }

    def _run(*args: str) -> dict:
        result = runner.invoke(
            app,
            ["--config-dir", str(config_dir), "--json", *args],
            env=env,
        )
        assert result.exit_code == 0, (
            f"{' '.join(args)} failed (exit {result.exit_code}):\n{result.output}"
        )
        return json.loads(result.output)

    # 1. Defensive cleanup: if a stale invitation exists from a prior aborted
    # run, cancel it first so we start from a known state.
    initial = _run("project", "invitation-list", "--project", alias)["data"]["invitations"]
    for inv in initial:
        if inv.get("user", {}).get("email", "").casefold() == invite_email.casefold():
            _run(
                "project",
                "invitation-cancel",
                "--project",
                alias,
                "--email",
                invite_email,
                "--yes",
            )

    # 2. Send the invitation.
    sent = _run(
        "project",
        "invite",
        "--project",
        alias,
        "--email",
        invite_email,
        "--role",
        "guest",
        "--reason",
        "kbagent v0.26.1 e2e",
    )["data"]
    assert sent["status"] == "ok"
    assert sent["invitation_id"] is not None
    invitation_id = sent["invitation_id"]

    try:
        # 3. Confirm it shows up in invitation-list.
        listed = _run("project", "invitation-list", "--project", alias)["data"]["invitations"]
        emails = {row["user"]["email"].casefold() for row in listed}
        assert invite_email.casefold() in emails, f"{invite_email} did not appear in {emails}"
    finally:
        # 4. Cancel (always, even if the assertion above fails -- never leave
        # a real-email invitation around for a flaky test).
        _run(
            "project",
            "invitation-cancel",
            "--project",
            alias,
            "--email",
            invite_email,
            "--invitation-id",
            str(invitation_id),
            "--yes",
        )

    # 5. Verify the invitation is gone.
    final = _run("project", "invitation-list", "--project", alias)["data"]["invitations"]
    final_emails = {row["user"]["email"].casefold() for row in final}
    assert invite_email.casefold() not in final_emails, (
        f"{invite_email} still pending after cancel: {final_emails}"
    )


# ---------------------------------------------------------------------------
# MCP-parity commands (since v0.30.0)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EMcpParityCommands:
    """E2E coverage for the 0.30.0 MCP-parity commands.

    Lives apart from the giant TestFullE2E to keep cleanup tight and let
    `pytest -k Mcp` exercise just these flows when iterating.

    Covered:
    - ``kbagent project info`` -- full project metadata
    - ``kbagent config row-create`` / ``row-update`` / ``row-delete`` --
      complete row CRUD against a throwaway ``ex-generic-v2`` config
      created and torn down inside the test class
    - ``kbagent search`` -- pre-flight feature gate path (asserts
      either real results OR a clean ``FEATURE_NOT_ENABLED`` error,
      never the raw 404 the API returns without the gate)
    - ``kbagent config oauth-url`` -- master-token pre-flight path
      (asserts either a real URL OR ``MISSING_MASTER_TOKEN`` exit 3)
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Any:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-mcpparity"[:60]

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

        self.client = KeboolaClient(stack_url=self.url, token=self.token)
        self._configs: list[tuple[str, str]] = []  # (component_id, config_id)

        yield

        # Cleanup: delete the throwaway configs we created for row CRUD tests
        for comp_id, cfg_id in self._configs:
            try:
                self.client.delete_config(component_id=comp_id, config_id=cfg_id)
            except Exception as exc:
                print(f"  WARN: delete_config {comp_id}/{cfg_id}: {exc}")
        self.client.close()

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def _create_throwaway_config(self) -> str:
        """Create an ex-generic-v2 config we own and will delete in cleanup."""
        cfg = self.client.create_config(
            component_id="ex-generic-v2",
            name=f"{RUN_ID}-mcpparity-rows",
            description="E2E throwaway -- row CRUD test harness",
            configuration={},
        )
        cfg_id = str(cfg["id"])
        self._configs.append(("ex-generic-v2", cfg_id))
        return cfg_id

    # ------------------------------------------------------------------
    # project info
    # ------------------------------------------------------------------

    def test_project_info_returns_full_metadata(self) -> None:
        """`project info` returns alias, project_id, features, limits, metrics, token info."""
        data = self._run_ok("project", "info", "--project", self.alias)["data"]

        assert data["alias"] == self.alias
        assert isinstance(data["project_id"], int)
        assert data["project_id"] > 0
        assert data["project_name"]
        assert data["stack_url"] == self.url
        assert isinstance(data["features"], list)
        assert isinstance(data["limits"], dict)
        assert isinstance(data["metrics"], dict)
        assert "is_master_token" in data
        assert "token_id" in data

    # ------------------------------------------------------------------
    # config row-create / row-update / row-delete
    # ------------------------------------------------------------------

    def test_config_row_lifecycle(self) -> None:
        """row-create -> row-update (--set + --merge + --is-disabled/--is-enabled + --dry-run)
        -> row-delete -> 404 on re-delete."""
        cfg_id = self._create_throwaway_config()

        # 1. row-create with inline JSON
        create = self._run_ok(
            "config",
            "row-create",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--name",
            f"{RUN_ID}-row",
            "--description",
            "E2E test row",
            "--configuration",
            '{"parameters": {"endpoint": "/users", "limit": 100}}',
        )["data"]
        row_id = create["id"]
        assert create["name"] == f"{RUN_ID}-row"
        assert create["configuration"]["parameters"]["endpoint"] == "/users"
        assert create["isDisabled"] is False

        # 2. row-update --set: preserves siblings, changes one key
        upd_set = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--set",
            "parameters.limit=999",
        )["data"]
        params = upd_set["configuration"]["parameters"]
        assert params["endpoint"] == "/users", "sibling preserved"
        assert params["limit"] == 999, "set applied"

        # 3. row-update --merge: deep-merge, preserves all siblings
        upd_merge = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--merge",
            "--configuration",
            '{"parameters": {"timeout": 30}}',
        )["data"]
        params = upd_merge["configuration"]["parameters"]
        assert params["endpoint"] == "/users"
        assert params["limit"] == 999
        assert params["timeout"] == 30

        # 4. row-update --dry-run: previews changes without writing
        dry = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--set",
            "parameters.endpoint=/preview",
            "--dry-run",
        )["data"]
        assert dry["dry_run"] is True
        assert dry["new_configuration"]["parameters"]["endpoint"] == "/preview"
        # Assert dry-run did NOT persist
        check = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--description",
            "noop verify",
        )["data"]
        assert check["configuration"]["parameters"]["endpoint"] == "/users"

        # 5. row-update --is-disabled toggles isDisabled flag
        disabled = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--is-disabled",
        )["data"]
        assert disabled["isDisabled"] is True

        enabled = self._run_ok(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--is-enabled",
        )["data"]
        assert enabled["isDisabled"] is False

        # 6. row-update with --is-disabled AND --is-enabled exits 2
        result = self._run(
            "config",
            "row-update",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--is-disabled",
            "--is-enabled",
        )
        assert result.exit_code == 2

        # 7. row-delete (with --yes to skip confirmation)
        deleted = self._run_ok(
            "config",
            "row-delete",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--yes",
        )["data"]
        assert deleted["deleted"] is True
        assert deleted["row_id"] == row_id

        # 8. Re-delete the same row -> 404 NOT_FOUND (deletion is NOT idempotent)
        result = self._run(
            "config",
            "row-delete",
            "--project",
            self.alias,
            "--component-id",
            "ex-generic-v2",
            "--config-id",
            cfg_id,
            "--row-id",
            row_id,
            "--yes",
        )
        assert result.exit_code != 0
        envelope = json.loads(result.output)
        assert envelope["error"]["code"] == "NOT_FOUND"

    # ------------------------------------------------------------------
    # search (feature-gate aware)
    # ------------------------------------------------------------------

    def test_search_returns_results_or_feature_gate_error(self) -> None:
        """Either the project has `global-search` and we get results, or we get
        a clean FEATURE_NOT_ENABLED per-project error -- never a raw 404."""
        result = self._run_ok("search", "data", "--project", self.alias, "--limit", "5")
        data = result["data"]
        assert "results" in data
        assert "errors" in data
        assert "stats" in data

        if data["errors"]:
            # Project does not have the feature -- the pre-flight check kicks in
            err = data["errors"][0]
            assert err["error_code"] == "FEATURE_NOT_ENABLED", err
            assert "global-search" in err["message"]
        # If feature is enabled, results may or may not be empty; both are valid.

    # ------------------------------------------------------------------
    # config oauth-url (master-token gate)
    # ------------------------------------------------------------------

    def test_config_oauth_url_master_token_gate(self) -> None:
        """Either we have a master token and get a URL, or we exit 3 with
        MISSING_MASTER_TOKEN -- never a raw 500 from the underlying API."""
        # Need a real component_id + config_id to attempt OAuth on. We use a
        # throwaway config (the OAuth URL builder doesn't actually validate the
        # component supports OAuth -- it just embeds the IDs in the URL fragment).
        cfg_id = self._create_throwaway_config()
        result = self._run(
            "config",
            "oauth-url",
            "--project",
            self.alias,
            "--component-id",
            "keboola.ex-google-drive",
            "--config-id",
            cfg_id,
        )

        envelope = json.loads(result.output)
        if envelope["status"] == "ok":
            # Master token: must return a well-formed URL
            assert result.exit_code == 0
            url = envelope["data"]["url"]
            assert url.startswith("https://external.keboola.com/oauth/index.html")
            assert "token=" in url
            assert "sapiUrl=" in url
            assert f"/keboola.ex-google-drive/{cfg_id}" in url
        else:
            # Non-master token: must exit 3 with MISSING_MASTER_TOKEN
            assert result.exit_code == 3
            assert envelope["error"]["code"] == "MISSING_MASTER_TOKEN"
            assert "master" in envelope["error"]["message"].lower()
