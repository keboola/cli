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
        # PHASE 13: Job commands (expanded)
        # ==============================================================

        _step(38, "job list + detail", "verify job listing structure")
        self._test_job_commands()

        # ==============================================================
        # PHASE 14: Cleanup
        # ==============================================================

        _step(39, "config delete", "cleanup config via CLI")
        self._test_config_delete(config_id)

        _step(40, "storage delete-table + delete-bucket", "CLI-driven cleanup")
        self._test_storage_cleanup(bucket_id, table_id)

        _step(41, "project edit + remove", "final cleanup")
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
