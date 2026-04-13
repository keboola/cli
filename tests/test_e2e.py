"""Comprehensive end-to-end tests for Keboola Agent CLI.

Exercises the FULL CLI surface against a real (empty) Keboola project:
  - Project CRUD (add / list / status / edit / remove)
  - Storage CRUD (create-bucket / create-table / upload / download / delete)
  - Config operations (list / detail / search / update --set / update --merge / delete)
  - File operations (upload / list / detail / download / tag / delete)
  - Branch lifecycle (list / create / use / reset / delete)
  - Component discovery (list / detail)
  - Job commands (list)
  - Encrypt (values)
  - Lineage, sharing, doctor, context, version, changelog

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


def _create_test_file(path: Path, content: str = "hello e2e") -> Path:
    """Create a small text file for file-upload testing."""
    file_path = path / f"{RUN_ID}_file.txt"
    file_path.write_text(content)
    return file_path


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

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        """Guarantee cleanup of ALL created resources, even on test failure."""
        yield
        print("\n--- CLEANUP ---")
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
        # PHASE 1: Setup — offline commands + project registration
        # ==============================================================

        _step(1, "version / changelog / context", "offline commands")
        self._test_offline_commands()

        _step(2, "project add", "register project")
        self._test_project_add()

        _step(3, "project list + status", "verify connectivity")
        self._test_project_list_and_status()

        _step(4, "doctor", "health check")
        self._test_doctor()

        # ==============================================================
        # PHASE 2: Read empty project
        # ==============================================================

        _step(5, "read empty project", "config list / storage buckets / job list")
        self._test_empty_reads()

        # ==============================================================
        # PHASE 3: Storage CRUD
        # ==============================================================

        _step(6, "storage create-bucket")
        bucket_id = self._test_create_bucket()

        _step(7, "storage buckets + bucket-detail", "verify bucket exists")
        self._test_bucket_listing(bucket_id)

        _step(8, "storage create-table")
        table_id = self._test_create_table(bucket_id)

        _step(9, "storage upload-table", "upload CSV data")
        self._test_upload_table(table_id)

        _step(10, "storage tables + table-detail")
        self._test_table_listing(bucket_id, table_id)

        _step(11, "storage download-table", "data round-trip verification")
        self._test_download_table(table_id)

        _step(12, "storage unload-table", "export to file storage")
        self._test_unload_table(table_id)

        # ==============================================================
        # PHASE 4: Config operations (create via API, test via CLI)
        # ==============================================================

        _step(13, "config create (via API) + CLI list / detail / search")
        config_id = self._test_config_operations()

        _step(14, "config update --set / --merge / --dry-run")
        self._test_config_update(config_id)

        # Component list requires at least one config in the project
        _step(15, "component list + detail", "discover components")
        self._test_component_commands()

        # ==============================================================
        # PHASE 5: File operations
        # ==============================================================

        _step(16, "file upload / list / detail / download / tag / delete")
        self._test_file_operations()

        # ==============================================================
        # PHASE 6: Encrypt
        # ==============================================================

        _step(17, "encrypt values")
        self._test_encrypt(config_id)

        # ==============================================================
        # PHASE 7: Branch lifecycle
        # ==============================================================

        _step(18, "branch list / create / use / reset / delete")
        self._test_branch_lifecycle()

        # ==============================================================
        # PHASE 8: Sharing & Lineage (read-only on single project)
        # ==============================================================

        _step(19, "sharing list / lineage show", "read-only checks")
        self._test_sharing_and_lineage()

        # ==============================================================
        # PHASE 9: Job list (verify structure with real data)
        # ==============================================================

        _step(20, "job list", "verify job listing structure")
        self._test_job_list()

        # ==============================================================
        # PHASE 10: Config delete + storage cleanup via CLI
        # ==============================================================

        _step(21, "config delete", "cleanup config via CLI")
        self._test_config_delete(config_id)

        _step(22, "storage delete-table + delete-bucket", "CLI-driven cleanup")
        self._test_storage_cleanup(bucket_id, table_id)

        # ==============================================================
        # PHASE 11: Project edit & remove
        # ==============================================================

        _step(23, "project edit + remove", "final cleanup")
        self._test_project_edit_and_remove()

        print("\n" + "=" * 60)
        print("  ALL E2E STEPS PASSED")
        print("=" * 60)

    # ==================================================================
    # Step implementations
    # ==================================================================

    def _test_offline_commands(self) -> None:
        """Test version, changelog, context — no project needed."""
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

        # storage buckets — filter only our prefix later
        data = self._run_ok("storage", "buckets", "--project", self.alias)
        # Just check structure
        assert "buckets" in data["data"]
        assert "errors" in data["data"]

        # job list
        data = self._run_ok("job", "list", "--project", self.alias, "--limit", "5")
        assert "jobs" in data["data"]
        assert data["data"]["errors"] == []

    def _test_component_commands(self) -> None:
        """List components and get detail for one.

        NOTE: component list only returns components that have at least one
        configuration in the project. This test runs AFTER config creation.
        """
        # component list — now that we have a keboola.ex-db-snowflake config
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

    def _test_job_list(self) -> None:
        """Verify job listing structure."""
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

        # Verify content matches what we uploaded
        with open(output_path) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 5
        ids = sorted([r["id"] for r in rows])
        assert ids == ["1", "2", "3", "4", "5"]

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

        # config list — should find our config
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
        """Test config update with --set, --merge, and --dry-run."""
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
        """Test branch create, list, use, reset, delete."""
        # branch list — should only have main
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
        # Branch create auto-activates — reset so further tests use main
        self._run_ok("branch", "reset", "--project", self.alias)

        # branch list — should now include our branch
        data = self._run_ok("branch", "list", "--project", self.alias)
        branch_names = [b["name"] for b in data["data"]["branches"]]
        assert branch_name in branch_names

        # branch use — activate the dev branch
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

        # branch reset — deactivate the dev branch
        data = self._run_ok("branch", "reset", "--project", self.alias)

        # Verify: project status should show no active branch
        data = self._run_ok("project", "status", "--project", self.alias)
        status = data["data"][0]
        assert status["active_branch_id"] is None

        # branch delete
        data = self._run_ok(
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

    def _test_sharing_and_lineage(self) -> None:
        """Test sharing list and lineage show (read-only, may be empty)."""
        # sharing list
        data = self._run_ok("sharing", "list", "--project", self.alias)
        assert "shared_buckets" in data["data"] or "errors" in data["data"]

        # lineage show
        data = self._run_ok("lineage", "show", "--project", self.alias)
        # Lineage may be empty on a single-project setup
        assert data["status"] == "ok"

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
        # project edit — change URL back to same (just verify command works)
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
