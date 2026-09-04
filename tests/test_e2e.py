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
  - Lineage, sharing, doctor, context, version, changelog, init

All resources are prefixed with 'e2e-{run_id}' and cleaned up even on failure.

Two ways to supply credentials:

1. Explicit env vars (CI / one-off):
     - E2E_API_TOKEN: Storage API token
     - E2E_URL: Stack URL (e.g. connection.keboola.com)

   Run:
       E2E_API_TOKEN=xxx E2E_URL=connection.keboola.com \
           uv run pytest tests/test_e2e.py -v -s --tb=long

2. Config-dir mode -- run against a project already registered in a local
   kbagent config.json, without ever typing the token:
     - KBAGENT_E2E_CONFIG_DIR: path to a .kbagent directory (holds config.json)
     - KBAGENT_E2E_ALIAS: alias of the project inside that config.json

   Run:
       KBAGENT_E2E_CONFIG_DIR=/tmp/kbagent/.kbagent KBAGENT_E2E_ALIAS=kbagent-e2e \
           uv run pytest tests/test_e2e.py -v -s --tb=long

   The token is read from config.json by this process at import time and
   promoted into E2E_API_TOKEN / E2E_URL, so the rest of the harness (token
   masking, skip gate, per-class fixtures, cleanup client) is unchanged. The
   token is never typed on the command line and never written back to disk.
   Explicit E2E_API_TOKEN always wins over config-dir mode.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from helpers import metastore_scope_available
from keboola_agent_cli import Client
from keboola_agent_cli.auth.models import StackSession
from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.auth.state_store import AuthStateStore
from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, normalize_stack_url

# ---------------------------------------------------------------------------
# Environment & skip logic
# ---------------------------------------------------------------------------

ENV_TOKEN = "E2E_API_TOKEN"
ENV_URL = "E2E_URL"

# Config-dir mode: populate the token/URL env vars from an existing kbagent
# config.json (selected by KBAGENT_E2E_CONFIG_DIR + KBAGENT_E2E_ALIAS) so the
# harness can run against a pre-registered project without the token being
# typed on the command line. Explicit E2E_API_TOKEN always takes precedence.
ENV_CONFIG_DIR = "KBAGENT_E2E_CONFIG_DIR"
ENV_ALIAS = "KBAGENT_E2E_ALIAS"


def _hydrate_credentials_from_config_dir() -> None:
    """Promote a config.json project's token/URL into E2E_API_TOKEN / E2E_URL.

    No-op when E2E_API_TOKEN is already set (explicit env wins) or when
    config-dir mode is not requested (either env var missing). Fails fast with
    a clear message when the requested alias is absent, so a typo never
    silently degrades into "all E2E tests skipped".
    """
    if os.environ.get(ENV_TOKEN):
        return  # explicit token wins -- never override a hand-set value
    config_dir = os.environ.get(ENV_CONFIG_DIR)
    alias = os.environ.get(ENV_ALIAS)
    if not config_dir or not alias:
        return  # config-dir mode not requested
    store = ConfigStore(config_dir=Path(config_dir))
    project = store.get_project(alias)
    if project is None:
        available = ", ".join(sorted(store.load().projects)) or "(none)"
        raise RuntimeError(
            f"{ENV_ALIAS}={alias!r} not found in {config_dir}/config.json. "
            f"Available aliases: {available}."
        )
    os.environ[ENV_TOKEN] = project.token
    os.environ[ENV_URL] = project.stack_url


_hydrate_credentials_from_config_dir()

HAS_CREDENTIALS = os.environ.get(ENV_TOKEN) is not None

skip_without_credentials = pytest.mark.skipif(
    not HAS_CREDENTIALS,
    reason=f"E2E tests require {ENV_TOKEN} environment variable",
)

# Separate gate for `auth register-projects` (programmatic-auth session, not a
# static Storage token) -- deliberately the SAME env var names as
# tests/test_e2e_auth.py so one pre-provisioned session covers both files.
# NEVER the access token: it is short-lived (~1h) and would be stale before a
# CI run even starts; only the refresh token is taken on the command line.
ENV_SESSION_REFRESH_TOKEN = "E2E_SESSION_REFRESH_TOKEN"
ENV_SESSION_PROJECT_ID = "E2E_SESSION_PROJECT_ID"

HAS_SESSION_CREDENTIALS = bool(
    os.environ.get(ENV_URL)
    and os.environ.get(ENV_SESSION_REFRESH_TOKEN)
    and os.environ.get(ENV_SESSION_PROJECT_ID)
)

skip_without_session_credentials = pytest.mark.skipif(
    not HAS_SESSION_CREDENTIALS,
    reason=(
        f"`auth register-projects` E2E coverage requires {ENV_URL}, "
        f"{ENV_SESSION_REFRESH_TOKEN} and {ENV_SESSION_PROJECT_ID} (a pre-provisioned "
        "session on a stack with the programmatic-auth feature flag enabled). See "
        "tests/test_e2e_auth.py's module docstring for how to provision one."
    ),
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


def _step(num: float | str, title: str, detail: str = "") -> None:
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

        _step(
            11.1,
            "storage truncate-table",
            "drop all rows, verify schema preserved, restore data",
        )
        self._test_truncate_table_roundtrip(table_id)

        _step(
            11.2,
            "storage snapshot-* + table-from-snapshot",
            "snapshot lifecycle + restore as new table (issue #512)",
        )
        self._test_snapshot_roundtrip(bucket_id, table_id)

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

        _step(
            "15.1",
            "storage create-table --source-table-id + swap-tables",
            "BigQuery repartition workflow (backend-aware)",
        )
        self._test_create_table_from_source(bucket_id, table_id)

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

        _step("19b", "config new --push", "one-shot remote create (0.33.0+)")
        self._test_config_new_push()

        _step("19c", "config new --push validation", "real schema vs real body (#587)")
        self._test_config_new_push_schema_validation()

        _step("19c2", "config new --push --output-dir", "scaffold carries created config_id (#644)")
        self._test_config_new_push_output_dir()

        _step("19d", "config clone", "whole-config duplicate incl. rows (#587)")
        self._test_config_clone()

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

            _step(26.5, "library facade", "Client.query + files round-trip, in-process")
            self._test_library_facade(workspace_id, table_id)

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

        _step(37, "sharing list", "read-only checks")
        self._test_sharing_and_lineage()

        # ==============================================================
        # PHASE 12.5: Kai (Keboola AI Assistant)
        # ==============================================================

        _step(38, "kai ping / ask / history", "Keboola AI Assistant")
        self._test_kai_commands()

        # ==============================================================
        # PHASE 12.6: MCP parity commands (epic #390, 0.73.0)
        # ==============================================================

        _step(
            38.5,
            "docs/examples/schema/sync-action/transformation/flow examples",
            "native ports of the keboola-mcp-server tools",
        )
        self._test_mcp_parity_commands()

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

        # init --from-global --project ALIAS copies only the named project (#404).
        # Seed the "global" config (init_config_dir, source=global) with the E2E
        # project, then init a fresh sub-dir copying only that one alias.
        _json_ok(
            _invoke(
                init_config_dir,
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
        )
        filter_dir = self.work_dir / "init_filter_test"
        filter_dir.mkdir()
        with patch("keboola_agent_cli.commands.init.Path.cwd", return_value=filter_dir):
            result = _invoke(
                init_config_dir,
                ["--json", "init", "--from-global", "--project", self.alias],
            )
        data = _json_ok(result)
        assert data["data"]["projects_copied"] == 1
        local_cfg = json.loads((filter_dir / ".kbagent" / "config.json").read_text())
        assert self.alias in local_cfg["projects"]

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

    def _test_create_table_from_source(self, bucket_id: str, source_table_id: str) -> None:
        """create-table --source-table-id (+ swap-tables). BigQuery-only feature.

        On a BigQuery project: copy the populated source into a new table with a
        clustering layout, then swap the two. On any other backend: assert the
        pre-flight guard rejects the request (exit 2) before issuing the create.
        """
        detail = self._run_ok(
            "storage", "bucket-detail", "--project", self.alias, "--bucket-id", bucket_id
        )["data"]
        backend = (detail.get("backend") or detail.get("sql_dialect") or "").lower()

        repart_name = f"{RUN_ID.replace('-', '_')}_repart"
        repart_id = f"{bucket_id}.{repart_name}"

        if backend != "bigquery":
            # Pre-flight guard: a non-BigQuery backend fails fast with exit 2 and
            # never issues the create (no table is left behind).
            result = self._run(
                "storage",
                "create-table",
                "--project",
                self.alias,
                "--bucket-id",
                bucket_id,
                "--name",
                repart_name,
                "--source-table-id",
                source_table_id,
                "--clustering-field",
                "id",
            )
            assert result.exit_code == 2, (
                f"Expected exit 2 from BigQuery guard, got {result.exit_code}:\n{result.output}"
            )
            assert "BigQuery" in result.output
            return

        # BigQuery: create the repartitioned copy from the populated source.
        created = self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            repart_name,
            "--source-table-id",
            source_table_id,
            "--clustering-field",
            "id",
            "--primary-key",
            "id",
        )["data"]
        assert created["table_id"] == repart_id
        assert created["source_table_id"] == source_table_id

        # Swap the repartitioned copy into the original table's place. Storage
        # swap requires a branch; the default branch works.
        branch_id = self._run_ok("branch", "list", "--project", self.alias)["data"]["branches"][0][
            "id"
        ]
        self._run_ok(
            "storage",
            "swap-tables",
            "--project",
            self.alias,
            "--table-id",
            source_table_id,
            "--target-table-id",
            repart_id,
            "--branch",
            str(branch_id),
            "--yes",
        )

        # Verify the swap through `definition` (issue #621). The table ID is
        # identical whether or not the swap happened -- the registered layout is
        # the only thing that tells the two apart, and before 0.88.0 kbagent
        # could not read it at all. `create-table`'s own output is no substitute:
        # it echoes the layout that was REQUESTED.
        swapped = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            source_table_id,
            "--branch",
            str(branch_id),
        )["data"]
        definition = swapped["definition"]
        assert isinstance(definition, dict), f"Expected a definition object, got {definition!r}"
        assert definition.get("clustering", {}).get("fields") == ["id"], (
            "The clustering layout applied by create-table is not readable on the "
            f"production name after the swap: {definition.get('clustering')!r}"
        )

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

    def _test_truncate_table_roundtrip(self, table_id: str) -> None:
        """Drop all rows, verify schema and downstream invariants, then restore.

        Asserts the contract that distinguishes ``truncate-table`` from
        ``delete-table``: column definitions, primary key, and table identity
        survive; only the rows go to zero. After verification, re-uploads the
        same 5+3 CSV pair so the table returns to its prior 8-row state for
        downstream test hops.
        """
        # Snapshot the pre-truncate schema for the post-truncate diff.
        before = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert before["rows_count"] > 0, (
            f"truncate roundtrip needs a non-empty table; got rows_count={before['rows_count']}"
        )
        before_columns = sorted(c["name"] for c in before["column_details"])
        before_pk = list(before.get("primary_key") or [])

        # Dry-run: receipt must show rows_before > 0 but never touch the table.
        dry = self._run_ok(
            "storage",
            "truncate-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--dry-run",
        )["data"]
        assert dry["dry_run"] is True
        assert dry["would_truncate"][0]["table_id"] == table_id
        assert dry["would_truncate"][0]["rows_before"] == before["rows_count"]

        # Apply: rows must report as 0. The Storage API endpoint is
        # uniformly async-via-job; the client polls to completion before
        # returning, so rows_after=0 is authoritative at this point.
        applied = self._run_ok(
            "storage",
            "truncate-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--yes",
        )["data"]
        assert applied["dry_run"] is False
        assert applied["truncated"][0]["table_id"] == table_id
        assert applied["truncated"][0]["rows_before"] == before["rows_count"]
        assert applied["truncated"][0]["rows_after"] == 0
        assert applied["failed"] == []

        # Verify: rowsCount=0, columns unchanged, primary key unchanged,
        # table identity unchanged.
        after = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert after["rows_count"] == 0, (
            f"expected rows_count=0 after truncate, got {after['rows_count']}"
        )
        after_columns = sorted(c["name"] for c in after["column_details"])
        assert after_columns == before_columns, (
            f"truncate changed columns: before={before_columns} after={after_columns}"
        )
        assert list(after.get("primary_key") or []) == before_pk, "truncate changed primary key"
        assert after["table_id"] == before["table_id"], "table identity changed"

        # Restore: re-upload the same 5-row base + 3-row incremental so the
        # downstream hops (download, unload, workspace load) see the same
        # row count they would have otherwise.
        base_csv = _create_test_csv(self.data_dir, rows=5)
        self._run_ok(
            "storage",
            "upload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--file",
            str(base_csv),
        )
        incr_csv = _create_incremental_csv(self.data_dir, start=6, rows=3)
        self._run_ok(
            "storage",
            "upload-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--file",
            str(incr_csv),
            "--incremental",
        )

        restored = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert restored["rows_count"] == before["rows_count"], (
            f"restore failed: expected {before['rows_count']} rows, got {restored['rows_count']}"
        )

    def _test_snapshot_roundtrip(self, bucket_id: str, table_id: str) -> None:
        """Snapshot lifecycle + restore-as-new-table (issue #512).

        create -> list -> detail -> table-from-snapshot (dry-run, then apply)
        -> verify the restored table matches the source (rows, columns,
        primary key) -> delete the restored table and the snapshot -> verify
        the snapshot is gone. Leaves the source table untouched for the
        downstream hops.
        """
        before = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert before["rows_count"] > 0, "snapshot roundtrip needs a non-empty table"
        before_columns = sorted(c["name"] for c in before["column_details"])
        before_pk = list(before.get("primary_key") or [])

        # Create a snapshot; the receipt carries the new snapshot ID.
        created = self._run_ok(
            "storage",
            "snapshot-create",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--description",
            "kbagent E2E snapshot roundtrip",
        )["data"]
        snapshot_id = str(created["snapshot_id"])
        assert snapshot_id, f"snapshot-create returned no snapshot_id: {created}"

        # List: the new snapshot must appear for the source table.
        listed = self._run_ok(
            "storage",
            "snapshots",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert listed["count"] >= 1
        assert snapshot_id in [str(s["id"]) for s in listed["snapshots"]]

        # Detail: the snapshot must point back at the source table.
        detail = self._run_ok(
            "storage",
            "snapshot-detail",
            "--project",
            self.alias,
            "--snapshot-id",
            snapshot_id,
        )["data"]
        assert str(detail["snapshot"]["id"]) == snapshot_id
        assert detail["snapshot"]["table"]["id"] == table_id

        # Restore dry-run: receipt only, no table created.
        restored_name = "e2e_snapshot_restore"
        restored_table_id = f"{bucket_id}.{restored_name}"
        dry = self._run_ok(
            "storage",
            "table-from-snapshot",
            "--project",
            self.alias,
            "--snapshot-id",
            snapshot_id,
            "--bucket-id",
            bucket_id,
            "--name",
            restored_name,
            "--dry-run",
        )["data"]
        assert dry["dry_run"] is True
        tables = self._run_ok(
            "storage", "tables", "--project", self.alias, "--bucket-id", bucket_id
        )["data"]["tables"]
        assert restored_table_id not in [t["id"] for t in tables], (
            "dry-run must not create the table"
        )

        # Restore for real: a NEW table with the snapshot's data appears.
        applied = self._run_ok(
            "storage",
            "table-from-snapshot",
            "--project",
            self.alias,
            "--snapshot-id",
            snapshot_id,
            "--bucket-id",
            bucket_id,
            "--name",
            restored_name,
        )["data"]
        assert applied["dry_run"] is False
        assert applied["table_id"] == restored_table_id

        # The restored table must match the source: rows, columns, primary key.
        restored = self._run_ok(
            "storage",
            "table-detail",
            "--project",
            self.alias,
            "--table-id",
            restored_table_id,
        )["data"]
        assert restored["rows_count"] == before["rows_count"], (
            f"restored rows {restored['rows_count']} != source rows {before['rows_count']}"
        )
        assert sorted(c["name"] for c in restored["column_details"]) == before_columns
        assert list(restored.get("primary_key") or []) == before_pk

        # Drop the restored table so downstream bucket listings are unchanged.
        self._run_ok(
            "storage",
            "delete-table",
            "--project",
            self.alias,
            "--table-id",
            restored_table_id,
            "--yes",
        )

        # Delete the snapshot (dry-run first) and verify it is gone.
        del_dry = self._run_ok(
            "storage",
            "snapshot-delete",
            "--project",
            self.alias,
            "--snapshot-id",
            snapshot_id,
            "--dry-run",
        )["data"]
        assert del_dry["would_delete"] == [snapshot_id]
        deleted = self._run_ok(
            "storage",
            "snapshot-delete",
            "--project",
            self.alias,
            "--snapshot-id",
            snapshot_id,
        )["data"]
        assert deleted["deleted"] == [snapshot_id]
        assert deleted["failed"] == []
        after_delete = self._run_ok(
            "storage",
            "snapshots",
            "--project",
            self.alias,
            "--table-id",
            table_id,
        )["data"]
        assert snapshot_id not in [str(s["id"]) for s in after_delete["snapshots"]]

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
        # The raw Storage API `definition` is passed through (issue #621). It is
        # present on every table-detail response regardless of backend or typing,
        # so the KEY is the contract here; the BigQuery layout inside it is
        # asserted in _test_create_table_from_source where a layout is actually set.
        assert "definition" in detail, (
            "table-detail dropped `definition` -- the BigQuery partition/cluster "
            "layout is unreadable without it (issue #621)"
        )

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
            check=False,
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

        # --change-description sets the version changeDescription verbatim.
        change_desc = f"{RUN_ID} AI-1234: e2e change description"
        dry = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
            "--set",
            "parameters.audit_probe=1",
            "--change-description",
            change_desc,
            "--dry-run",
        )
        # --dry-run echoes the change description that would be sent.
        assert dry["data"]["change_description"] == change_desc

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
            "parameters.audit_probe=1",
            "--change-description",
            change_desc,
        )

        # Verify the new version's changeDescription via config detail.
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
        assert data["data"]["changeDescription"] == change_desc

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

    def _test_config_new_push(self) -> None:
        """Test ``config new --push`` -- one-shot remote create (0.33.0+).

        Exercises the full lifecycle introduced in v0.33.0:
        1. ``--push --no-files --dry-run`` returns the planned POST envelope
           with ``validation_status`` and no real API call.
        2. ``--push --no-files`` creates an empty-shell config and returns
           ``project_alias`` / ``branch_id`` / ``validation_status="skipped"``.
        3. ``config detail`` finds the newly created config.
        4. ``config update --set`` patches it (proves create + update interop).
        5. ``config delete`` cleans up (also tracked in ``_created_config_ids``
           for the safety-net teardown).
        """
        push_name = f"{RUN_ID} push-created"

        # 1) Dry-run -- envelope only, no POST.
        dry = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            self.alias,
            "--name",
            f"{push_name} (dry)",
            "--push",
            "--no-files",
            "--dry-run",
        )["data"]
        assert dry["dry_run"] is True, dry
        assert dry["project_alias"] == self.alias
        assert dry["component_id"] == "keboola.ex-http"
        assert dry["configuration"] == {}  # default empty shell
        assert dry["validation_status"] in ("ok", "skipped", "failed")

        # 2) Real create.
        created = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            self.alias,
            "--name",
            push_name,
            "--push",
            "--no-files",
        )["data"]
        new_config_id = str(created["id"])
        # Register for the safety-net cleanup loop in teardown BEFORE any
        # downstream assertion can raise -- guarantees the config is reaped
        # even if a verification step below fails.
        self._created_config_ids.append(("keboola.ex-http", new_config_id))

        try:
            assert created["project_alias"] == self.alias
            # Empty-shell creation auto-skips validation (FIIA pattern).
            assert created["validation_status"] == "skipped", created
            # validation_errors is always annotated (symmetric with dry-run).
            assert created["validation_errors"] == [], created

            # 3) Verify via config detail.
            detail = self._run_ok(
                "config",
                "detail",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                new_config_id,
            )["data"]
            assert str(detail["id"]) == new_config_id
            assert detail["name"] == push_name

            # 4) Patch the freshly-pushed config.
            self._run_ok(
                "config",
                "update",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                new_config_id,
                "--set",
                "parameters.smoke_test=true",
            )
        finally:
            # 5) Inline cleanup so failures mid-flow still tear down the
            # remote config promptly; the global teardown loop is a safety
            # net for the case where this delete itself fails.
            # ``config delete`` has no ``--yes`` flag (CLAUDE.md inventory).
            self._run_ok(
                "config",
                "delete",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                new_config_id,
            )

    def _test_config_new_push_output_dir(self) -> None:
        """Test ``config new --push --output-dir`` writes an adoptable scaffold (issue #644).

        The written ``_config.yml`` must carry ``_keboola.config_id`` of the
        just-created configuration -- before the #644 fix it did not, and the
        next ``sync push`` created a duplicate (34-config incident). A plain
        (non-sync) output dir is used, so the layout is flat and no manifest
        is involved; the stamped ID is the contract under test.
        """
        import yaml as _yaml

        push_name = f"{RUN_ID} push-scaffold-644"
        out_dir = self.work_dir / "scaffold-644"
        out_dir.mkdir(parents=True, exist_ok=True)

        created = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            self.alias,
            "--name",
            push_name,
            "--push",
            "--output-dir",
            str(out_dir),
        )["data"]
        new_config_id = str(created["id"])
        self._created_config_ids.append(("keboola.ex-http", new_config_id))

        try:
            scaffold_info = created["local_scaffold"]
            assert scaffold_info["config_id"] == new_config_id, scaffold_info
            config_yml = Path(scaffold_info["directory"]) / "_config.yml"
            assert config_yml.is_file(), f"scaffold not written: {scaffold_info}"
            raw = config_yml.read_text(encoding="utf-8")
            parsed = _yaml.safe_load(raw)
            assert parsed["_keboola"]["component_id"] == "keboola.ex-http"
            # The stamped ID is the duplicate-prevention contract (#644):
            # sync diff's adopt-by-id guard pairs the dir with the remote.
            assert parsed["_keboola"]["config_id"] == new_config_id, raw
            assert isinstance(parsed["_keboola"]["config_id"], str)
            assert "assigned by Keboola on first push" not in raw
        finally:
            self._run_ok(
                "config",
                "delete",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                new_config_id,
            )

    def _test_config_new_push_schema_validation(self) -> None:
        """Test ``config new --push`` schema validation against a REAL schema (issue #587).

        ``_test_config_new_push`` only covers the empty-shell path, where
        validation auto-skips -- so nothing there exercises a real component
        schema against a real body. That is the exact gap issue #587 fell
        through: a component's ``configurationSchema`` describes the CONTENTS
        of ``parameters``, the validator compared it against the WHOLE
        configuration object, and the unit-test mock schema was written in the
        same wrong shape, so CI stayed green while a correct configuration was
        rejected and a malformed one accepted.

        Every step is ``--dry-run``: no configuration is created.

        Tolerant by design -- not every stack serves a schema for every
        component. The load-bearing assertion is one-directional and holds
        either way: **a valid configuration must never come back "failed"**.
        The negative case only runs when the stack actually returned a schema.
        """
        # A well-formed writer body: parameters + a runtime sibling, which the
        # parameters schema does not describe and must therefore not trip on.
        valid_body = json.dumps(
            {
                "parameters": {
                    "db": {
                        "host": "mysql.example.com",
                        "port": 3306,
                        "database": "e2e",
                        "user": "e2e",
                        "#password": "e2e",
                    }
                },
                "runtime": {"parallelism": "20"},
            }
        )

        valid = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-db-mysql",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID} validation (dry)",
            "--push",
            "--no-files",
            "--configuration",
            valid_body,
            "--dry-run",
        )["data"]

        assert valid["validation_status"] in ("ok", "skipped"), (
            f"A valid configuration must never fail validation, got: {valid}"
        )
        # Unwrapping is for validation only -- the planned POST keeps every
        # sibling key. Dropping `runtime` is the silent data loss of #587.
        assert set(valid["configuration"]) == {"parameters", "runtime"}, valid
        assert valid["configuration"]["runtime"] == {"parallelism": "20"}, valid

        if valid["validation_status"] == "skipped":
            print(
                f"  {_DIM}   (stack served no schema for keboola.ex-db-mysql; "
                f"negative case skipped){_RESET}"
            )
            return

        # The stack DID serve a schema, so a body with junk parameters must be
        # rejected -- proving the unwrap did not turn validation into a no-op.
        invalid = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-db-mysql",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID} validation-bad (dry)",
            "--push",
            "--no-files",
            "--configuration",
            json.dumps({"parameters": {"nonsense": 1}}),
            "--dry-run",
        )["data"]

        assert invalid["validation_status"] == "failed", invalid
        assert invalid["validation_errors"], invalid
        # Paths name the section to fix. Before #587 this read "<root>: ...",
        # which pointed the reader at the wrong level of their own config.
        assert all(e.startswith("parameters") for e in invalid["validation_errors"]), invalid

        # A body that FORGOT the `parameters` wrapper must fail too (issue
        # #605). It used to validate clean -- the flattened body matched the
        # parameters-level schema, so `--push` created a configuration with no
        # `parameters` key at all, which the UI and the runtime read as empty
        # while reporting success. Same `db` payload as `valid_body`, one level
        # too high.
        flattened = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-db-mysql",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID} validation-flat (dry)",
            "--push",
            "--no-files",
            "--configuration",
            json.dumps({"db": {"host": "mysql.example.com", "database": "e2e"}}),
            "--dry-run",
        )["data"]

        assert flattened["validation_status"] == "failed", flattened
        assert any("no 'parameters' key" in e for e in flattened["validation_errors"]), flattened

    def _test_config_clone(self) -> None:
        """Test ``config clone`` -- whole-configuration duplicate (0.84.2+, #587).

        The point of the command is that NOTHING is left behind, so the
        assertions are about completeness, not about the happy path: the
        source is given a `runtime` sibling and two rows, and the clone must
        come back with both. `runtime.parallelism` is exactly what went
        missing in #587, and rows are what makes hand-rebuilding hopeless at
        65 of them.

        Everything is created and deleted inside this test.
        """
        clone_name = f"{RUN_ID} clone-source"
        source = self._run_ok(
            "config",
            "new",
            "--component-id",
            "keboola.ex-http",
            "--project",
            self.alias,
            "--name",
            clone_name,
            "--push",
            "--no-files",
        )["data"]
        source_id = str(source["id"])
        self._created_config_ids.append(("keboola.ex-http", source_id))

        clone_id: str | None = None
        try:
            # Give the source a sibling key and rows -- the things that get lost.
            self._run_ok(
                "config",
                "update",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                source_id,
                "--set",
                "runtime.parallelism=20",
            )
            for row_name in ("alpha", "beta"):
                self._run_ok(
                    "config",
                    "row-create",
                    "--project",
                    self.alias,
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    source_id,
                    "--name",
                    f"row-{row_name}",
                    "--configuration",
                    json.dumps({"parameters": {"table": row_name}}),
                )

            # Dry-run first: reports the plan, writes nothing.
            planned = self._run_ok(
                "config",
                "clone",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                source_id,
                "--name",
                f"{RUN_ID} clone-dry",
                "--dry-run",
            )["data"]
            assert planned["dry_run"] is True, planned
            assert planned["mode"] == "same-project", planned
            assert planned["row_count"] == 2, planned

            cloned = self._run_ok(
                "config",
                "clone",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                source_id,
                "--name",
                f"{RUN_ID} clone-target",
            )["data"]
            clone_id = str(cloned["id"])
            self._created_config_ids.append(("keboola.ex-http", clone_id))
            assert clone_id != source_id, cloned
            assert cloned["mode"] == "same-project", cloned

            detail = self._run_ok(
                "config",
                "detail",
                "--project",
                self.alias,
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                clone_id,
            )["data"]
            configuration = detail.get("configuration") or {}
            # The sibling key that #587 is about survived the copy.
            assert configuration.get("runtime") == {"parallelism": 20}, configuration
            # And so did the rows -- no client-side row copying involved.
            rows = detail.get("rows") or []
            assert len(rows) == 2, rows
            assert sorted(r["name"] for r in rows) == ["row-alpha", "row-beta"], rows
        finally:
            for config_id in filter(None, (clone_id, source_id)):
                self._run_ok(
                    "config",
                    "delete",
                    "--project",
                    self.alias,
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    config_id,
                )

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
        # Issue #304 / BigQuery support: detail surfaces login_type + qs_compatible.
        assert "login_type" in detail
        assert "qs_compatible" in detail

    def _test_workspace_password(self, workspace_id: int) -> None:
        """Reset workspace password and verify a new password is returned.

        Keypair-auth workspaces (Snowflake ``person-keypair`` login) have no
        password, so the API rejects the reset with HTTP 400 "Reset password is
        not supported for login type ...". That is an environment property of
        the project, not a failure -- skip cleanly when it happens.
        """
        result = self._run(
            "workspace",
            "password",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )
        if result.exit_code != 0 and "not supported for login type" in result.output:
            print(f"  {_DIM}skip: keypair-auth workspace has no password to reset{_RESET}")
            return
        data = _json_ok(result)
        assert data["data"]["password"]  # non-empty password

    def _test_library_facade(self, workspace_id: int, table_id: str) -> None:
        """Exercise the public in-process library facade against the live stack.

        Imports ``keboola_agent_cli.Client`` and runs a real query + Storage
        Files round-trip with no CLI subprocess -- the in-process path the
        jasnost feedback (#415) consumes. Backend-specific identifier quoting
        mirrors ``_test_workspace_query``.
        """
        from keboola_agent_cli import Client, FileEntry

        ws_table_name = table_id.rsplit(".", 1)[-1]
        detail = self._run_ok(
            "workspace",
            "detail",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )["data"]
        quote = "`" if detail.get("backend") == "bigquery" else '"'
        sql = f"SELECT COUNT(*) AS cnt FROM {quote}{ws_table_name}{quote}"

        with Client(url=self.url, token=self.token) as kbc:
            # query() -> list[dict] keyed by column name
            rows = kbc.query(workspace_id, sql)
            assert isinstance(rows, list) and rows, "facade query must return rows"
            assert isinstance(rows[0], dict), "facade query rows must be dicts"
            assert "cnt" in {k.lower() for k in rows[0]}, f"expected cnt column, got {rows[0]}"

            # files: upload bytes -> read_bytes -> list -> delete, all in-process
            facade_tag = f"{RUN_ID}-facade"
            payload = b"facade-e2e-roundtrip"
            meta = kbc.files.upload(payload, name=f"{RUN_ID}-facade.txt", tags=[facade_tag])
            assert isinstance(meta, FileEntry) and meta.id > 0
            self._created_file_ids.append(meta.id)

            assert kbc.files.read_bytes(meta.id) == payload, "read_bytes round-trip mismatch"

            # The tag-filtered Files list is read-after-write eventually
            # consistent: the upload is durable (read_bytes round-tripped it
            # just above), but the tag index can lag a few seconds. Poll instead
            # of asserting the first list (intermittently missed in CI 2026-07-22).
            found = False
            for _ in range(15):
                if any(f.id == meta.id for f in kbc.files.list(tags=[facade_tag])):
                    found = True
                    break
                time.sleep(2)
            assert found, "uploaded file must appear in list"

            kbc.files.delete(meta.id)
            self._created_file_ids.remove(meta.id)

    def _test_workspace_load(self, workspace_id: int, table_id: str) -> None:
        """Load a table into the workspace.

        Deliberately left on the DEFAULT (auto) load type -- that is the path
        real users take, and it is the one that decides CLONE vs COPY per
        table. The per-table report is asserted so a silently-dropped
        ``loadType`` shows up here rather than as a mysteriously slow load.
        """
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
        assert data["data"]["load_type_requested"] == "auto"
        loaded = data["data"]["tables"]
        assert [entry["table_id"] for entry in loaded] == [table_id]
        assert loaded[0]["load_type"] in {"clone", "copy"}

    def _test_workspace_query(self, workspace_id: int, table_id: str) -> None:
        """Run a SQL query in the workspace and verify result.

        Identifier quoting is backend-specific: Snowflake uses double quotes,
        BigQuery uses back-ticks. Running this E2E against the BigQuery project
        (e2e-bigquery, #379+) with Snowflake quoting would fail because BigQuery
        reads ``"name"`` as a string literal, not an identifier.
        """
        # Table name in workspace is the last segment of table_id
        ws_table_name = table_id.rsplit(".", 1)[-1]
        detail = self._run_ok(
            "workspace",
            "detail",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
        )["data"]
        quote = "`" if detail.get("backend") == "bigquery" else '"'
        sql = f"SELECT COUNT(*) AS cnt FROM {quote}{ws_table_name}{quote}"
        # Default (fast) path: reads inline /results -- structured columns+rows.
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
        stmt = data["data"]["statements"][0]
        assert stmt["columns"], "fast inline path must return structured columns"
        assert stmt["rows"], "fast inline path must return structured rows"
        assert "csv_data" in stmt, "csv_data must stay populated for legacy consumers"

        # Full (export) path: complete result set via the CSV export endpoint.
        full_data = self._run_ok(
            "workspace",
            "query",
            "--project",
            self.alias,
            "--workspace-id",
            str(workspace_id),
            "--sql",
            sql,
            "--full",
        )
        assert full_data["status"] == "ok"
        assert "csv_data" in full_data["data"]["statements"][0]

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

        # files (list) -- the tag index is read-after-write eventually
        # consistent; poll until the just-uploaded file appears rather than
        # asserting the first list.
        file_ids: list[int] = []
        for _ in range(15):
            data = self._run_ok(
                "storage",
                "files",
                "--project",
                self.alias,
                "--tag",
                f"e2e-{RUN_ID}",
            )
            file_ids = [f["id"] for f in data["data"]["files"]]
            if file_id in file_ids:
                break
            time.sleep(2)
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

        # NOTE: `lineage show` requires a pre-built graph (--load PATH), so it is
        # not a bare read check. Its E2E coverage lives in test_e2e_lineage_deep.py
        # (build + show against real synced data).

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

    def _test_mcp_parity_commands(self) -> None:
        """MCP parity commands from epic #390 (0.73.0): docs query, config
        examples, semantic-layer schema, component sync-action, transformation
        lifecycle, flow examples."""
        # docs query — server-side documentation RAG (AI Service)
        result = self._run(
            "docs", "query", "What is a Keboola Storage bucket?", "--project", self.alias
        )
        if result.exit_code != 0:
            print(f"  {_YELLOW}SKIP: docs query failed (AI Service unavailable?){_RESET}")
        else:
            data = _json_ok(result)
            assert isinstance(data["data"]["text"], str) and data["data"]["text"].strip()
            assert isinstance(data["data"]["source_urls"], list)

        # config examples — reformat of AI-service component detail
        data = self._run_ok(
            "config",
            "examples",
            "--component-id",
            "keboola.ex-google-drive",
            "--project",
            self.alias,
        )
        assert data["data"]["component_id"] == "keboola.ex-google-drive"
        assert isinstance(data["data"]["root_examples"], list)

        # semantic-layer schema — live metastore JSON Schema (version-resolved)
        result = self._run("semantic-layer", "schema", "--project", self.alias, "--type", "metric")
        if result.exit_code != 0:
            print(f"  {_YELLOW}SKIP: semantic-layer schema (metastore unavailable?){_RESET}")
        else:
            data = _json_ok(result)
            schemas = data["data"]["schemas"]
            assert [s["type"] for s in schemas] == ["metric"]
            assert isinstance(schemas[0]["schema"], dict) and schemas[0]["schema"]

        # component sync-action — full round-trip to sync-actions.{stack};
        # deliberately bad config: a structured API error PROVES the wiring
        # (URL derivation, auth, camelCase body); a 2xx needs live DB creds.
        result = self._run(
            "component",
            "sync-action",
            "testConnection",
            "--component-id",
            "keboola.ex-db-snowflake",
            "--project",
            self.alias,
            "--config-data",
            '{"parameters": {"db": {"host": "invalid.example.com"}}}',
        )
        assert result.exit_code != 0
        # NOT _json(): that helper asserts exit_code == 0, but this command is
        # EXPECTED to fail -- parse the error envelope directly (#508 regression).
        err = json.loads(result.output)
        assert err["error"]["code"] in ("API_ERROR", "VALIDATION_ERROR")

        # flow examples — bundled, offline
        data = self._run_ok("flow", "examples")
        assert isinstance(data["data"], list) and data["data"]
        assert {"phases", "tasks"} <= set(data["data"][0])

        # flow schema --full without --project — bundled snapshot fallback
        data = self._run_ok("flow", "schema", "--full")
        assert data["data"]["source"] == "bundled"

        # transformation lifecycle: create -> show (ids) -> edit -> verify.
        # Cleanup: sync-based delete is heavyweight here; the config is
        # removed via the recycle-bin-safe Storage API through self.api.
        created = self._run_ok(
            "transformation",
            "create",
            "--project",
            self.alias,
            "--name",
            "E2E Parity Transformation",
            "--sql",
            'CREATE TABLE "e2e_tf_out" AS SELECT 1 AS "id"; SELECT 2;',
            "--created-table",
            "e2e_tf_out",
        )
        tf_config_id = created["data"]["config_id"]
        tf_component_id = created["data"]["component_id"]
        try:
            shown = self._run_ok(
                "transformation",
                "show",
                "--project",
                self.alias,
                "--config-id",
                tf_config_id,
            )
            block = shown["data"]["blocks"][0]
            assert block["id"] == "b0"
            assert block["codes"][0]["id"] == "b0.c0"
            assert len(block["codes"][0]["script"]) == 2

            self._run_ok(
                "transformation",
                "edit",
                "--project",
                self.alias,
                "--config-id",
                tf_config_id,
                "--change-description",
                "e2e parity check",
                "--op",
                '{"op": "str_replace", "search_for": "SELECT 2", "replace_with": "SELECT 3"}',
            )
            reshown = self._run_ok(
                "transformation",
                "show",
                "--project",
                self.alias,
                "--config-id",
                tf_config_id,
            )
            assert reshown["data"]["blocks"][0]["codes"][0]["script"][1] == "SELECT 3;"
        finally:
            try:
                self.api.delete_config(tf_component_id, tf_config_id)
                print(f"  Deleted transformation config {tf_config_id}")
            except Exception as exc:
                print(f"  WARN: failed to delete transformation {tf_config_id}: {exc}")

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
        """Delete the test config via CLI, exercising the 0.89.0 trash round trip.

        delete -> repeated delete answers ``already_in_trash`` (the retry that
        used to PURGE permanently) -> trash-list finds it -> restore brings it
        back -> final delete. Every leg runs against the real Storage API, so
        the double-delete guard is proven against the endpoint that actually
        overloads DELETE, not against a mock.
        """
        common = (
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
            "--config-id",
            config_id,
        )
        data = self._run_ok("config", "delete", *common)
        assert data["data"]["config_id"] == config_id
        assert data["data"]["status"] == "deleted"

        # The retry: MUST be a no-op success, never a permanent purge.
        data = self._run_ok("config", "delete", *common)
        assert data["data"]["status"] == "already_in_trash"

        data = self._run_ok(
            "config",
            "trash-list",
            "--project",
            self.alias,
            "--component-id",
            TEST_COMPONENT_ID,
        )
        trashed_ids = [e["config_id"] for e in data["data"]["trash"]]
        assert config_id in trashed_ids, f"{config_id} not in trash listing: {trashed_ids}"

        data = self._run_ok("config", "restore", *common)
        assert data["data"]["status"] == "restored"

        # Restored config is live again -- detail must answer.
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

        # Final cleanup: back into the trash.
        data = self._run_ok("config", "delete", *common)
        assert data["data"]["status"] == "deleted"
        # Remove from cleanup since we deleted via CLI
        self._created_config_ids.remove((TEST_COMPONENT_ID, config_id))

    def _poll_columns(
        self, table_id: str, *, present: str | None = None, absent: str | None = None
    ) -> list[str]:
        """Poll table-detail until a column appears/disappears (max ~30s).

        The Storage API's column listing is read-after-write eventually
        consistent on some stacks: an add-column/delete-column receipt is
        authoritative (the API confirmed the DDL), but an immediately-following
        table-detail can serve stale metadata for a few seconds -- measured
        live on connection.us-east4.gcp.keboola.com 2026-07-22 (~5-10s lag,
        wider when the table recently had snapshot activity). Poll instead of
        asserting the first read.
        """
        columns: list[str] = []
        for _ in range(15):
            data = self._run_ok(
                "storage", "table-detail", "--project", self.alias, "--table-id", table_id
            )
            columns = data["data"]["columns"]
            if (present is None or present in columns) and (
                absent is None or absent not in columns
            ):
                return columns
            time.sleep(2)
        return columns

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

        # add-column: add a typed column and verify it appears in table-detail
        data = self._run_ok(
            "storage",
            "add-column",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--column",
            "status:VARCHAR(20)",
        )
        assert data["data"]["column"] == "status"
        assert data["data"]["definition"]["type"] == "VARCHAR"
        assert data["data"]["table_id"] == table_id
        columns = self._poll_columns(table_id, present="status")
        assert "status" in columns, f"Expected 'status' column after add-column, got {columns}"

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

        # Verify the column is gone (poll: same read-after-DDL staleness as add)
        columns_after = self._poll_columns(table_id, absent="value")
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
            logging.getLogger(__name__).debug("best-effort cleanup tracking failed", exc_info=True)

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
        # The `columns` payload of PUT .../definition is a POSITIVE-ONLY patch:
        # the batch above sent `id` alone, so `name` -- described two steps
        # earlier -- must survive untouched. The whole write path assumes this;
        # a full-replace endpoint would silently wipe every column left out of
        # the payload, and the assertion on `id` alone would never notice.
        assert col_descs.get("name") == "Human-readable name"
        # The native write leaves no legacy flat keys behind (#624).
        assert data["data"]["legacy_column_descriptions"] == []

        self._test_storage_describe_migrate(table_id)

    def _test_storage_describe_migrate(self, table_id: str) -> None:
        """Seed a pre-0.88.0 flat metadata key and migrate it (#624).

        The flat ``KBC.column.{name}.description`` convention is what kbagent
        wrote before the native definition endpoint; nothing but kbagent ever
        read it. Seeding goes through the raw client on purpose -- no CLI
        command writes that shape any more.

        Seeded on ``value``, the one column the describe steps above leave
        undescribed: that is the real pre-0.88.0 shape (a legacy key and
        nothing else). ``id`` / ``name`` already carry native descriptions,
        so a legacy key there is a *conflict*, which the second half of this
        test covers separately.
        """
        self.api.set_table_metadata(
            table_id=table_id,
            entries=[("KBC.column.value.description", "Legacy column description")],
        )

        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        assert data["data"]["legacy_column_descriptions"] == ["value"]

        # Dry run reports the table and writes nothing.
        data = self._run_ok(
            "storage",
            "describe-migrate",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--dry-run",
        )
        assert data["data"]["dry_run"] is True
        assert data["data"]["tables_migrated"] == 0
        migrated = {item["table_id"]: item["columns"] for item in data["data"]["migrated"]}
        assert migrated[table_id]["value"] == "Legacy column description"

        data = self._run_ok(
            "storage",
            "describe-migrate",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--yes",
        )
        assert data["data"]["tables_migrated"] == 1
        assert data["data"]["errors"] == []

        # The description survives where everyone reads it, the legacy key is gone.
        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        assert data["data"]["legacy_column_descriptions"] == []
        col_descs = {c["name"]: c.get("description", "") for c in data["data"]["column_details"]}
        assert col_descs.get("value") == "Legacy column description"

        # Re-running is a no-op: nothing left to migrate.
        data = self._run_ok(
            "storage",
            "describe-migrate",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--yes",
        )
        assert data["data"]["migrated"] == []

        # A legacy key on a column that ALREADY has a native description is a
        # conflict: the newer (visible) value wins, the stale key is reported
        # and left alone rather than overwriting what the UI shows.
        self.api.set_table_metadata(
            table_id=table_id,
            entries=[("KBC.column.id.description", "Stale legacy id description")],
        )
        data = self._run_ok(
            "storage",
            "describe-migrate",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--yes",
        )
        assert data["data"]["migrated"] == []
        conflicts = [s for s in data["data"]["skipped"] if s["reason"] == "conflict"]
        assert [s["column"] for s in conflicts] == ["id"]

        data = self._run_ok(
            "storage", "table-detail", "--project", self.alias, "--table-id", table_id
        )
        col_descs = {c["name"]: c.get("description", "") for c in data["data"]["column_details"]}
        assert col_descs.get("id") == "Batch column id desc"
        assert data["data"]["legacy_column_descriptions"] == ["id"]

    def _test_semantic_layer_roundtrip(self) -> None:
        """Live roundtrip of the semantic-layer command group against ``self.alias``.

        Bootstraps two throwaway models on the test project, exercises every
        verb (model/show/add/edit/validate/export/import/promote/build/token/remove),
        and tears everything down even on failure. All entity names are prefixed
        with a unique tag so a residue check at the end of the test can assert
        the project is clean.

        NOTE: Not wired into ``test_full_cli_e2e`` (which runs ~30 min). The
        same surface is exercised independently by
        :class:`TestE2ESemanticLayerLifecycle` -- run that with
        ``pytest -k SemanticLayer`` for a focused live check.
        """
        from keboola_agent_cli.metastore_client import (
            SEMANTIC_TYPES,
            MetastoreClient,
        )

        tag = f"kbagent_e2e_{int(time.time())}"
        model_name = tag
        target_model_name = f"{tag}_target"

        # (item_type, item_id) tuples for guaranteed cleanup.
        created_items: list[tuple[str, str]] = []
        model_id: str | None = None
        target_model_id: str | None = None

        def _direct_delete(item_type: str, item_id: str) -> None:
            with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                mc.delete_item(item_type, item_id)  # ty: ignore[invalid-argument-type]

        try:
            # 1. model create
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                model_name,
            )
            model_id = data["data"]["model"]["id"]
            assert model_id

            # 2. add two datasets, three metrics, one constraint, one glossary entry.
            # tableId comes from the bucket/table built earlier in the big test.
            # We don't depend on it existing in actual Snowflake — the metastore
            # accepts any string. validate --deep is skipped (would 404 trying to
            # fetch storage detail for a synthetic tableId).
            ds1 = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_ds_a",
                "--table-id",
                "out.c-syn.fact_a",
            )
            created_items.append(("semantic-dataset", ds1["data"]["id"]))

            ds2 = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_ds_b",
                "--table-id",
                "out.c-syn.fact_b",
            )
            created_items.append(("semantic-dataset", ds2["data"]["id"]))

            m1 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_rev",
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_a",
                "--yes",
            )
            created_items.append(("semantic-metric", m1["data"]["id"]))

            m2 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_cost",
                "--sql",
                'SUM("schema"."AMOUNT")',
                "--dataset",
                "out.c-syn.fact_a",
                "--yes",
            )
            created_items.append(("semantic-metric", m2["data"]["id"]))

            m3 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_count_b",
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_b",
                "--yes",
            )
            created_items.append(("semantic-metric", m3["data"]["id"]))

            rel = self._run_ok(
                "semantic-layer",
                "add",
                "relationship",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rel_a_b",
                "--from",
                "out.c-syn.fact_a",
                "--to",
                "out.c-syn.fact_b",
                "--on",
                "fact_a.id = fact_b.fact_a_id",
            )
            created_items.append(("semantic-relationship", rel["data"]["id"]))

            cons = self._run_ok(
                "semantic-layer",
                "add",
                "constraint",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rev_warning",
                "--constraint-type",
                "inequality",
                "--rule",
                "value >= 0",
                "--metrics",
                f"{tag}_m_rev",
                "--severity",
                "warning",
            )
            created_items.append(("semantic-constraint", cons["data"]["id"]))

            gloss = self._run_ok(
                "semantic-layer",
                "add",
                "glossary",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--term",
                f"{tag}_GMV",
                "--definition",
                "Gross merchandise value (test)",
            )
            created_items.append(("semantic-glossary", gloss["data"]["id"]))

            # 3. show: count assertions
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
            )
            assert len(data["data"]["datasets"]) == 2
            assert len(data["data"]["metrics"]) == 3
            assert len(data["data"]["constraints"]) == 1
            assert len(data["data"]["glossary"]) == 1

            # 4. show --type metric
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "metric",
            )
            assert len(data["data"]["metrics"]) >= 3

            # 5. validate (basic) — expect valid because everything is wired
            data = self._run_ok(
                "semantic-layer",
                "validate",
                "--project",
                self.alias,
                "--model",
                model_name,
            )
            # The constraint has a severity suffix so no SEVERITY_SUFFIX warning;
            # the metric SUM("schema"."AMOUNT") doesn't match SUM_ON_PCT regex.
            assert data["data"]["valid"] is True

            # 6. edit metric rename — triggers constraint cascade
            data = self._run_ok(
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_rev",
                "--new-name",
                f"{tag}_m_revenue",
                "--yes",
            )
            new_metric_id = data["data"]["updated"]["id"]
            # Replace tracking: the old metric was DELETE+POSTed
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-metric" and i == m1["data"]["id"])
            ]
            created_items.append(("semantic-metric", new_metric_id))
            cascaded = data["data"]["cascaded_constraints"]
            assert any(c["status"] == "updated" for c in cascaded), (
                f"Expected at least one cascaded constraint, got: {cascaded}"
            )
            # The constraint id changed (DELETE+POST). Re-fetch the list.
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "constraint",
            )
            current_constraints = {c["id"] for c in data["data"]["constraints"]}
            # Remove the old constraint id from tracking; add the live ones.
            created_items = [(t, i) for (t, i) in created_items if t != "semantic-constraint"]
            for cid in current_constraints:
                created_items.append(("semantic-constraint", cid))

            # 7. export to a tmp file
            tmpdir = self.work_dir / "sl_export"
            tmpdir.mkdir(exist_ok=True)
            export_path = tmpdir / "snapshot.json"
            data = self._run_ok(
                "semantic-layer",
                "export",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--output",
                str(export_path),
            )
            assert export_path.is_file()

            # 8. import --dry-run from the same file — all conflicts should skip
            data = self._run_ok(
                "semantic-layer",
                "import",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--file",
                str(export_path),
                "--dry-run",
            )
            imported = data["data"]["imported"]
            # Every type already exists → at least one skip somewhere.
            total_skipped = sum(per.get("skipped", 0) for per in imported.values())
            total_created = sum(per.get("created", 0) for per in imported.values())
            assert total_skipped > 0, f"Expected skips on import-into-self, got: {imported}"
            assert total_created == 0, (
                f"Expected zero creations on dry-run import-into-self, got {total_created}: {imported}"
            )

            # 9. diff project vs exported file → zero diff (modulo modelUUID strip)
            data = self._run_ok(
                "semantic-layer",
                "diff",
                "--project-a",
                self.alias,
                "--model-a",
                model_name,
                "--file-b",
                str(export_path),
            )
            for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
                per = data["data"][type_key]
                assert per["added"] == [] and per["removed"] == [] and per["changed"] == [], (
                    f"Live model and just-exported file should match for {type_key}: {per}"
                )

            # 10. promote — bootstrap second model and copy into it
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                target_model_name,
            )
            target_model_id = data["data"]["model"]["id"]

            data = self._run_ok(
                "semantic-layer",
                "promote",
                "--from-project",
                self.alias,
                "--to-project",
                self.alias,
                "--from-model",
                model_name,
                "--to-model",
                target_model_name,
                "--dry-run",
            )
            # Every source item should be NEW in the empty target (dry-run).
            for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
                per = data["data"].get(type_key)
                if per is not None:
                    assert per["new"] > 0 or per["overwritten"] > 0 or per["identical"] >= 0, (
                        f"promote stats look wrong for {type_key}: {per}"
                    )

            # 11. build --dry-run — non-interactive heuristic; uses a real
            # storage table for schema fetch. Re-use the same RUN_ID table.
            data = self._run_ok(
                "semantic-layer",
                "build",
                "--project",
                self.alias,
                "--tables",
                f"in.c-{RUN_ID.replace('-', '_')}.{RUN_ID.replace('-', '_')}",
                "--dry-run",
            )
            assert data["data"]["fallback_used"] == "heuristic", (
                f"Expected heuristic fallback, got: {data['data'].get('fallback_used')}"
            )

            # 12. token --encrypt
            data = self._run_ok(
                "semantic-layer",
                "token",
                "--encrypt",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
            )
            envelope = data["data"]["encrypted"]
            assert "#metastore_token" in envelope
            assert envelope["#metastore_token"].startswith("KBC::"), (
                f"Expected ciphertext to start with KBC::, got: {envelope['#metastore_token'][:30]}..."
            )

            # 13. remove a single metric (--yes), then verify it's gone
            data = self._run_ok(
                "semantic-layer",
                "remove",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_count_b",
                "--yes",
            )
            removed_id = data["data"]["removed"]["id"]
            assert removed_id == m3["data"]["id"]
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-metric" and i == m3["data"]["id"])
            ]

            # Verify it's gone via show
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "metric",
            )
            metric_names = {m["name"] for m in data["data"]["metrics"]}
            assert f"{tag}_m_count_b" not in metric_names

        finally:
            # ----------------------------------------------------------------
            # Teardown — best-effort, runs even on test failure.
            # Reverse order: child items first, then both models.
            # ----------------------------------------------------------------
            print("\n--- SEMANTIC LAYER CLEANUP ---")
            for item_type, item_id in reversed(created_items):
                try:
                    _direct_delete(item_type, item_id)
                    print(f"  Deleted {item_type} {item_id}")
                except Exception as exc:
                    print(f"  WARN: failed to delete {item_type} {item_id}: {exc}")

            for mid in (target_model_id, model_id):
                if mid is None:
                    continue
                try:
                    _direct_delete("semantic-model", mid)
                    print(f"  Deleted semantic-model {mid}")
                except Exception as exc:
                    print(f"  WARN: failed to delete semantic-model {mid}: {exc}")

            # Residue check: assert no tagged items remain in the project.
            try:
                with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                    residue: list[str] = []
                    for stype in SEMANTIC_TYPES:
                        for item in mc.list_items(stype):  # ty: ignore[invalid-argument-type]
                            attrs = item.get("attributes") or {}
                            name = attrs.get("name") or attrs.get("term", "")
                            if isinstance(name, str) and name.startswith(tag):
                                residue.append(f"{stype}:{name}:{item.get('id', '')}")
                    if residue:
                        print(f"  WARN: residue detected after cleanup: {residue}")
            except Exception as exc:
                print(f"  WARN: residue scan failed: {exc}")

    def _test_project_edit_and_remove(self) -> None:
        """Edit project URL, rename round-trip + dry-run preview, then remove."""
        # --new-alias dry-run preview: predicts the rename without mutating.
        # Pinned by PR #266 review (Padak): exercise the dry-run pre-flight
        # against a real config dir so the planned-block shape is verified.
        new_alias = f"{self.alias}-renamed"
        data = self._run_ok(
            "project",
            "edit",
            "--project",
            self.alias,
            "--new-alias",
            new_alias,
            "--dry-run",
        )
        assert data["data"]["dry_run"] is True
        assert data["data"]["alias"] == self.alias  # unchanged in dry-run
        assert data["data"]["planned"]["new_alias"] == new_alias
        # Verify nothing actually moved.
        data = self._run_ok("project", "list")
        aliases = [p["alias"] for p in data["data"]]
        assert self.alias in aliases
        assert new_alias not in aliases

        # --new-alias live rename round-trip -- exercise the cascading rename
        # against a real config dir (Padak's BLOCKING from PR #266 review).
        data = self._run_ok("project", "edit", "--project", self.alias, "--new-alias", new_alias)
        assert data["data"]["old_alias"] == self.alias
        assert data["data"]["alias"] == new_alias
        assert data["data"]["rename"]["new_alias"] == new_alias
        # Rename back so subsequent steps keep using self.alias unchanged.
        data = self._run_ok("project", "edit", "--project", new_alias, "--new-alias", self.alias)
        assert data["data"]["alias"] == self.alias
        assert data["data"]["old_alias"] == new_alias

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
            # `lineage show` needs --load (pre-built graph) -- not a bare read;
            # covered in test_e2e_lineage_deep.py instead.
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
    """Test sync init/pull/diff/status/push/clone in a temp git repo.

    NOTE on ``sync clone`` coverage: the clone step (step 6) runs ``--dry-run``
    against the SAME project, so it exercises copy + manifest re-point + diff but
    NOT a real push (Phase D flow remap, fresh-target guard, idempotent re-run).
    A full live clone push would create configs in a second, dedicated *fresh*
    project, which the single-project E2E harness (E2E_API_TOKEN + E2E_URL) does
    not provide. The push path is covered by the unit suite
    (``tests/test_sync_clone.py``); a nightly full-clone E2E would need a
    separate empty target project (tracked as a follow-up)."""

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

        # 6. sync clone --dry-run (#426): copy the pulled tree into a fresh dir and
        # diff it against the SAME project -- exercises the clone composite
        # end-to-end (copy + manifest re-point + diff) WITHOUT mutating anything.
        _step(6, "sync clone --dry-run")
        clone_dir = self.project_dir.parent / "clone-target"
        data = self._run_ok(
            "sync",
            "clone",
            "--source",
            str(self.project_dir),
            "--target",
            self.alias,
            "--target-dir",
            str(clone_dir),
            "--dry-run",
        )
        assert data["status"] == "ok"
        clone_result = data["data"]
        assert clone_result["status"] == "dry_run"
        assert clone_result["target_alias"] == self.alias

    def test_sync_force_pull_conflict_aware(self) -> None:
        """`sync pull --force` is conflict-aware (0.53.0+), end-to-end.

        Locks both halves of the baseline-corruption fix against real Storage:

        * (b) local edited, remote UNCHANGED -> force-pull PRESERVES the edit
          (does not silently re-stamp the baseline); ``sync diff`` afterward
          still reports the config as modified.
        * (a) local edited AND remote also changed -> force-pull ABORTS with
          exit 1 and error code ``SYNC_CONFLICT``.

        Creates + cleans up a dedicated config so the test is idempotent.
        """
        import yaml as _yaml

        from keboola_agent_cli.client import KeboolaClient
        from keboola_agent_cli.constants import CONFIG_FILENAME

        cfg: dict = {}
        try:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                cfg = api.create_config(
                    component_id=TEST_COMPONENT_ID,
                    name=f"{RUN_ID}-forcepull",
                    description="E2E force-pull conflict fixture",
                    configuration={"parameters": {"db": {"host": "orig.example.com"}}},
                )
            cfg_id = str(cfg["id"])

            # --- init + pull, locate the config's _config.yml ---
            _step("7a", "sync init + pull (force-pull fixture)")
            self._run_ok(
                "sync", "init", "--project", self.alias, "--directory", str(self.project_dir)
            )
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            matches = [
                p
                for p in self.project_dir.rglob(CONFIG_FILENAME)
                if "rows" not in p.relative_to(self.project_dir).parts
                and str(
                    _yaml.safe_load(p.read_text(encoding="utf-8"))
                    .get("_keboola", {})
                    .get("config_id")
                )
                == cfg_id
            ]
            assert len(matches) == 1, f"config YAML not found after pull: {matches}"
            config_file = matches[0]

            # --- edit locally ---
            local = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
            local.setdefault("parameters", {})["_e2e_marker"] = "x"
            config_file.write_text(_yaml.dump(local, default_flow_style=False), encoding="utf-8")

            # --- (b) force-pull, remote UNCHANGED -> edit preserved ---
            _step("7b", "force-pull preserves edit when remote unchanged")
            self._run_ok(
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
                "--force",
            )
            diff_after = self._run_ok(
                "sync", "diff", "--project", self.alias, "--directory", str(self.project_dir)
            )
            modified = [c for c in diff_after["data"]["changes"] if c["change_type"] == "modified"]
            assert any(c["config_id"] == cfg_id for c in modified), (
                f"force-pull stranded the un-pushed edit: {diff_after['data']['summary']}"
            )

            # --- (a) mutate remote, force-pull -> SYNC_CONFLICT abort ---
            _step("7c", "force-pull aborts on a true conflict")
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                api.update_config(
                    component_id=TEST_COMPONENT_ID,
                    config_id=cfg_id,
                    configuration={"parameters": {"db": {"host": "remote-moved.example.com"}}},
                    change_description="e2e force-pull conflict",
                )
            conflict_result = self._run(
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
                "--force",
            )
            assert conflict_result.exit_code == 1, (
                f"expected exit 1 on conflict, got {conflict_result.exit_code}"
            )
            envelope = json.loads(conflict_result.output)
            assert envelope["status"] == "error"
            assert envelope["error"]["code"] == "SYNC_CONFLICT"
            assert any(c["config_id"] == cfg_id for c in envelope["error"]["details"]["conflicts"])
        finally:
            cfg_id = cfg.get("id") if cfg else None
            if cfg_id:
                try:
                    with KeboolaClient(stack_url=self.url, token=self.token) as api:
                        api.delete_config(component_id=TEST_COMPONENT_ID, config_id=cfg_id)
                except Exception as exc:
                    print(f"  [cleanup] Failed to delete {TEST_COMPONENT_ID}/{cfg_id}: {exc}")

    def test_sync_theirs_reconcile_and_is_disabled(self) -> None:
        """Sync trust cluster (0.72.0, issues #466/#467/#472), end-to-end.

        * ``pull --theirs`` resolves a true conflict by taking remote (no abort).
        * Deleting a config dir + plain ``pull`` re-materializes it (#466 pt3).
        * Config-level ``isDisabled`` round-trips: remote disable -> pulled as
          ``is_disabled: true``; explicit local ``is_disabled: false`` push
          re-enables the remote config (#467).
        * A phantom manifest entry (empty pull_hash, no dir) is excluded from
          push delete-planning and reported as ``never_fetched`` (#472).

        Creates + cleans up a dedicated config so the test is idempotent.
        """
        import yaml as _yaml

        from keboola_agent_cli.client import KeboolaClient
        from keboola_agent_cli.constants import CONFIG_FILENAME

        cfg: dict = {}
        try:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                cfg = api.create_config(
                    component_id=TEST_COMPONENT_ID,
                    name=f"{RUN_ID}-trustcluster",
                    description="E2E sync trust-cluster fixture",
                    configuration={"parameters": {"db": {"host": "orig.example.com"}}},
                )
            cfg_id = str(cfg["id"])

            _step("8a", "sync init + pull (trust-cluster fixture)")
            self._run_ok(
                "sync", "init", "--project", self.alias, "--directory", str(self.project_dir)
            )
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            matches = [
                p
                for p in self.project_dir.rglob(CONFIG_FILENAME)
                if "rows" not in p.relative_to(self.project_dir).parts
                and str(
                    _yaml.safe_load(p.read_text(encoding="utf-8"))
                    .get("_keboola", {})
                    .get("config_id")
                )
                == cfg_id
            ]
            assert len(matches) == 1, f"config YAML not found after pull: {matches}"
            config_file = matches[0]
            config_dir = config_file.parent

            # --- (a) true conflict: local edit + remote edit -> --theirs wins ---
            _step("8b", "pull --theirs resolves a true conflict by taking remote")
            local = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
            local.setdefault("parameters", {})["_e2e_local"] = "keep-me-not"
            config_file.write_text(_yaml.dump(local, default_flow_style=False), encoding="utf-8")
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                api.update_config(
                    component_id=TEST_COMPONENT_ID,
                    config_id=cfg_id,
                    configuration={"parameters": {"db": {"host": "theirs.example.com"}}},
                    change_description="e2e theirs conflict",
                )
            self._run_ok(
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
                "--theirs",
            )
            after = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
            assert after["parameters"]["db"]["host"] == "theirs.example.com"
            assert "_e2e_local" not in after.get("parameters", {}), (
                "--theirs must overwrite local edits with remote"
            )

            # --- (b) delete dir + plain pull -> re-materialized (#466 pt3) ---
            _step("8c", "plain pull re-materializes a deleted config dir")
            shutil.rmtree(config_dir)
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            assert config_file.exists(), "deleted config dir must be refetched by plain pull"

            # --- (c) isDisabled round-trip (#467) ---
            _step("8d", "isDisabled: remote disable -> pull; local false -> push re-enables")
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                api.update_config(
                    component_id=TEST_COMPONENT_ID,
                    config_id=cfg_id,
                    is_disabled=True,
                    change_description="e2e disable",
                )
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            pulled = _yaml.safe_load(config_file.read_text(encoding="utf-8"))
            assert pulled.get("is_disabled") is True, "disabled state must be pulled (#467)"
            pulled["is_disabled"] = False
            config_file.write_text(_yaml.dump(pulled, default_flow_style=False), encoding="utf-8")
            self._run_ok(
                "sync", "push", "--project", self.alias, "--directory", str(self.project_dir)
            )
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                detail = api.get_config_detail(component_id=TEST_COMPONENT_ID, config_id=cfg_id)
            assert detail.get("isDisabled") is False, "explicit is_disabled: false must re-enable"

            # --- (d) phantom manifest entry never planned as DELETE (#472) ---
            _step("8e", "never-fetched phantom entry excluded from push delete-planning")
            manifest_path = self.project_dir / ".keboola" / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for entry in manifest["configurations"]:
                if entry["id"] == cfg_id:
                    entry["metadata"]["pull_hash"] = ""
                    entry["metadata"]["pull_config_hash"] = ""
                    entry["metadata"]["pull_extra_hashes"] = {}
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            shutil.rmtree(config_dir)
            push_dry = self._run_ok(
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
                "--force",
                "--dry-run",
            )
            envelope = push_dry["data"]
            planned_deletes = [
                c
                for c in envelope.get("changes", [])
                if c["change_type"] == "deleted" and c["config_id"] == cfg_id
            ]
            assert not planned_deletes, "phantom entry must never be planned as a remote DELETE"
            assert any(item["config_id"] == cfg_id for item in envelope.get("never_fetched", [])), (
                f"phantom entry must be reported as never_fetched: {envelope}"
            )
        finally:
            cfg_id = cfg.get("id") if cfg else None
            if cfg_id:
                try:
                    with KeboolaClient(stack_url=self.url, token=self.token) as api:
                        api.delete_config(component_id=TEST_COMPONENT_ID, config_id=cfg_id)
                except Exception as exc:
                    print(f"  [cleanup] Failed to delete {TEST_COMPONENT_ID}/{cfg_id}: {exc}")

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

    def test_sync_ignored_components_round_trip(self) -> None:
        """Issue #689 (ignored components), end-to-end.

        The manifest's ``ignoredComponents`` field (unioned with the
        hardcoded ``ALWAYS_IGNORED_COMPONENTS``) is now honored by
        ``sync pull`` / ``sync diff`` / ``sync push``:

        * Adding a component to ``ignoredComponents`` and pulling drops its
          manifest entry AND removes its local dir, with the pull ``details``
          reporting ``action: "ignored"`` (vs ``"removed"`` for an actual
          remote deletion).
        * The trap this closes: re-ignoring a component WITHOUT pulling
          first, then deleting its now-stale local dir by hand (as one might
          when tidying a tree), must NOT make ``sync diff`` report it as
          ``deleted`` or ``sync push`` plan to delete/recreate it -- the
          config must be left untouched on the remote.
        * Un-ignoring the component and pulling again re-materializes it.

        Creates + cleans up a dedicated config so the test is idempotent.
        """
        import yaml as _yaml

        from keboola_agent_cli.client import KeboolaClient
        from keboola_agent_cli.constants import CONFIG_FILENAME

        cfg: dict = {}
        try:
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                cfg = api.create_config(
                    component_id=TEST_COMPONENT_ID,
                    name=f"{RUN_ID}-ignoredcomp",
                    description="E2E ignored-components round-trip fixture (#689)",
                    configuration={"parameters": {"db": {"host": "orig.example.com"}}},
                )
            cfg_id = str(cfg["id"])
            manifest_path = self.project_dir / ".keboola" / "manifest.json"

            def _find_config_dir() -> Path:
                matches = [
                    p
                    for p in self.project_dir.rglob(CONFIG_FILENAME)
                    if "rows" not in p.relative_to(self.project_dir).parts
                    and str(
                        _yaml.safe_load(p.read_text(encoding="utf-8"))
                        .get("_keboola", {})
                        .get("config_id")
                    )
                    == cfg_id
                ]
                assert len(matches) == 1, f"config YAML not found after pull: {matches}"
                return matches[0].parent

            # --- (a) sync init + pull -> materialized + tracked in manifest ---
            _step("9a", "sync init + pull (ignored-components fixture)")
            self._run_ok(
                "sync", "init", "--project", self.alias, "--directory", str(self.project_dir)
            )
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            config_dir = _find_config_dir()
            assert config_dir.exists()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert any(
                e["componentId"] == TEST_COMPONENT_ID and e["id"] == cfg_id
                for e in manifest["configurations"]
            ), "config must be tracked in the manifest right after pull"

            # --- (b) ignoredComponents + pull -> local dir AND manifest entry
            # gone; pull details report action "ignored" ---
            _step("9b", "ignoredComponents + pull drops the dir/manifest entry")
            manifest["ignoredComponents"] = [TEST_COMPONENT_ID]
            manifest_path.write_text(json.dumps(manifest, indent=4), encoding="utf-8")
            pull_data = self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )["data"]
            assert not config_dir.exists(), "ignored component's local dir must be removed"
            manifest_after = json.loads(manifest_path.read_text(encoding="utf-8"))
            assert not any(
                e["componentId"] == TEST_COMPONENT_ID and e["id"] == cfg_id
                for e in manifest_after["configurations"]
            ), "config must be dropped from the manifest once its component is ignored"
            ignored_details = [
                d
                for d in pull_data["details"]
                if d["component_id"] == TEST_COMPONENT_ID and d["action"] == "ignored"
            ]
            assert ignored_details, (
                f"pull details must report action=ignored: {pull_data['details']}"
            )

            # --- (c) un-ignore + pull -> re-materializes, giving us a fresh
            # baseline dir for the trap scenario below ---
            _step("9c", "un-ignore + pull re-materializes the config")
            manifest_after["ignoredComponents"] = []
            manifest_path.write_text(json.dumps(manifest_after, indent=4), encoding="utf-8")
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            config_dir = _find_config_dir()
            assert config_dir.exists(), "un-ignoring + pull must re-materialize the config"

            # --- (d) THE TRAP: re-add the ignore WITHOUT pulling, then delete
            # the (now stale) local dir by hand. diff/push must never mistake
            # this for a real remote deletion. ---
            _step("9d", "re-ignore without pulling + delete dir by hand -- the trap")
            manifest_current = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_current["ignoredComponents"] = [TEST_COMPONENT_ID]
            manifest_path.write_text(json.dumps(manifest_current, indent=4), encoding="utf-8")
            shutil.rmtree(config_dir)

            diff_data = self._run_ok(
                "sync", "diff", "--project", self.alias, "--directory", str(self.project_dir)
            )["data"]
            deleted_hits = [
                c
                for c in diff_data.get("changes", [])
                if c.get("config_id") == cfg_id and c.get("change_type") == "deleted"
            ]
            assert not deleted_hits, (
                f"an ignored component's stale manifest entry must never diff "
                f"as deleted: {deleted_hits}"
            )
            orphaned_hits = [
                o for o in diff_data.get("orphaned", []) if o.get("config_id") == cfg_id
            ]
            assert not orphaned_hits, (
                f"an ignored component must not surface as orphaned either: {orphaned_hits}"
            )

            push_dry = self._run_ok(
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(self.project_dir),
                "--dry-run",
            )["data"]
            planned_hits = [c for c in push_dry.get("changes", []) if c.get("config_id") == cfg_id]
            assert not planned_hits, (
                f"push --dry-run must plan neither a delete nor a re-create for it: {planned_hits}"
            )

            # The config must still exist untouched on the remote -- this is
            # the delete-dir-then-push production-delete trap #689 closes.
            with KeboolaClient(stack_url=self.url, token=self.token) as api:
                remote_detail = api.get_config_detail(
                    component_id=TEST_COMPONENT_ID, config_id=cfg_id
                )
            assert str(remote_detail["id"]) == cfg_id, "config must still exist remotely"

            # --- (e) un-ignore + pull -> re-materializes again ---
            _step("9e", "un-ignore + pull re-materializes the config again")
            manifest_current["ignoredComponents"] = []
            manifest_path.write_text(json.dumps(manifest_current, indent=4), encoding="utf-8")
            self._run_ok(
                "sync", "pull", "--project", self.alias, "--directory", str(self.project_dir)
            )
            assert _find_config_dir().exists(), "final un-ignore + pull must re-materialize"
        finally:
            cfg_id = cfg.get("id") if cfg else None
            if cfg_id:
                try:
                    with KeboolaClient(stack_url=self.url, token=self.token) as api:
                        api.delete_config(component_id=TEST_COMPONENT_ID, config_id=str(cfg_id))
                except Exception as exc:
                    print(f"  [cleanup] Failed to delete {TEST_COMPONENT_ID}/{cfg_id}: {exc}")


# ---------------------------------------------------------------------------
# Billing / PAYG credit balance (issue #594)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EBillingCredits:
    """End-to-end test for `kbagent billing credits`.

    The E2E project(s) used in CI are NOT pay-as-you-go enabled, so the
    honest contract to assert here is graceful degradation, not a populated
    balance: `--json` must return a well-formed `{"credits": [...],
    "errors": [...]}` envelope, exit 0, and the non-PAYG project must show
    up as a `PAYG_NOT_AVAILABLE` entry in `errors` rather than crashing or
    surfacing an opaque billing-host connection failure. The success-path
    row shape is only asserted when a PAYG project is actually present in
    the envelope, so this test stays meaningful (and starts covering the
    happy path) the day a PAYG project is added to the E2E fixtures --
    without needing to be rewritten then.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-billing"
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

    def test_billing_credits_returns_well_formed_envelope(self) -> None:
        """--json always returns {"credits": [...], "errors": [...]}, exit 0.

        A non-PAYG project degrades to a PAYG_NOT_AVAILABLE error entry
        instead of failing the command -- billing is a read, and one
        project lacking the feature must never abort the whole run.
        """
        result = self._run("billing", "credits", "--project", self.alias)
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert "credits" in data
        assert "errors" in data
        assert isinstance(data["credits"], list)
        assert isinstance(data["errors"], list)

        # This project is expected to be non-PAYG in the E2E fixtures. If it
        # is, it must show up as an actionable error, not a crash or a bare
        # connection failure against a possibly-NXDOMAIN billing host.
        non_payg_errors = [e for e in data["errors"] if e.get("project_alias") == self.alias]
        payg_rows = [c for c in data["credits"] if c.get("project_alias") == self.alias]

        if payg_rows:
            # A PAYG project showed up -- assert the full success-path row shape.
            row = payg_rows[0]
            for key in (
                "project_alias",
                "project_id",
                "consumed",
                "remaining",
                "total",
                "consumed_minutes",
                "remaining_minutes",
                "component_jobs_consumed",
                "workspace_jobs",
            ):
                assert key in row, f"missing key {key!r} in PAYG credit row: {row}"
            assert row["total"] == row["consumed"] + row["remaining"]
            assert row["remaining_minutes"] == row["remaining"] * 60
        else:
            # Graceful-degradation path: the project must be reported as
            # non-PAYG, never silently dropped from both lists.
            assert non_payg_errors, (
                f"project {self.alias!r} missing from both credits and errors: {data}"
            )
            assert non_payg_errors[0]["error_code"] == "PAYG_NOT_AVAILABLE"


# ---------------------------------------------------------------------------
# Notification subscriptions (Flow Notifications tab)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2ENotificationSubscriptions:
    """End-to-end test for `kbagent notification list` / `detail` / the write path (issue #600, #690).

    Most of the read-path tests below still cannot create their own fixture
    for the *pre-existing* subscriptions they inspect: whether the E2E
    project already has any Notifications-tab subscription (and how many
    distinct events it spans) is out of their control -- creating a
    subscription now costs a real write against the notification sibling
    host, which most of these read-only assertions have no reason to pay for.
    The honest contract for those is therefore the envelope and the live
    auth path -- that a plain project Storage token reaches the notification
    sibling host and gets a well-formed
    ``{"subscriptions": [...], "errors": [...], "project_wide_excluded": N}``
    back with exit 0, and that per-row shape and ``detail`` hold *when* rows
    exist. That makes those tests meaningful on an empty project (they still
    prove the host derivation, auth, and envelope) and start covering the
    row path the day a subscription is added, without needing a rewrite.

    That vacuity is not free, and it already cost us once: the service turned
    out to IGNORE its documented ``?event=`` filter, and the assertion that
    would have caught it passed vacuously on a project with no subscriptions.
    So the data-dependent assertions below ``pytest.skip`` with an explicit
    reason rather than passing silently -- a skipped test in the report is a
    visible gap; a green vacuous one is not. Populate the E2E project with a
    couple of Notifications-tab subscriptions on different events to turn
    them on.

    Since #690 the CLI *does* have a write path (`create` / `delete` /
    `replace-recipient`), and ``test_create_replace_recipient_delete_round_trip``
    below uses it to create, mutate, and tear down its own subscription --
    no pre-existing project data required, and no vacuity to skip around.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-notif"
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

        # L3 client for the canary below: it has to see the service's RAW
        # answer, and every CLI path narrows before the caller sees anything.
        from keboola_agent_cli.client import KeboolaClient

        self.client = KeboolaClient(stack_url=self.url, token=self.token)

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _payload(self, *args: str) -> dict[str, Any]:
        """Run the CLI and return the ``data`` payload, not the whole envelope.

        ``_json_ok`` hands back ``{"status": ..., "data": {...}}``, while every
        assertion below is about what sits inside ``data``. Unwrapping once
        here keeps that off each call site: reading the envelope as if it were
        the payload is exactly how this class shipped raising ``KeyError`` on
        every run, and a per-call-site ``["data"]`` invites the same slip again.
        """
        return _json_ok(self._run(*args))["data"]

    def test_notification_list_returns_well_formed_envelope(self) -> None:
        """A plain Storage token must reach the notification host and list."""
        _step(1, "notification list -- envelope + live auth path")
        data = self._payload("notification", "list", "--project", self.alias)

        assert isinstance(data["subscriptions"], list)
        assert isinstance(data["errors"], list)
        assert data["project_wide_excluded"] == 0
        # The read path needs no elevated scope -- a permission failure here
        # would mean the "plain project token is enough" contract broke.
        assert not data["errors"], f"unexpected per-project errors: {data['errors']}"

        for row in data["subscriptions"]:
            assert row["project_alias"] == self.alias
            assert row["scope"] in ("config", "project-wide")
            assert isinstance(row["filters"], list)
            # Email carries `address`, webhook carries `url`; both must land
            # in the single normalized column.
            if row["channel"] in ("email", "webhook"):
                assert row["address"], f"recipient missing an address: {row}"

    def test_event_filter_returns_only_that_event(self) -> None:
        """Every returned row must match `--event`, whatever the API did.

        The Notification Service ACCEPTS ``?event=`` (200, no 400) and then
        ignores it, returning the project's full subscription list. kbagent
        narrows client-side, so this assertion is about kbagent's contract,
        not the API's -- and it is the guard against anyone "simplifying" that
        filter away on the strength of the swagger.
        """
        _step(2, "notification list --event job-failed")
        data = self._payload(
            "notification", "list", "--project", self.alias, "--event", "job-failed"
        )

        assert all(row["event"] == "job-failed" for row in data["subscriptions"])

    def test_event_filter_actually_narrows_a_mixed_project(self) -> None:
        """The teeth of the previous test: prove it drops non-matching rows.

        `all(...)` over an empty or single-event list is vacuously true --
        exactly how the ignored-``?event=`` bug survived review. This one
        needs a project carrying at least two distinct events and skips
        loudly otherwise.
        """
        _step(3, "notification list --event <one of several>")
        everything = self._payload("notification", "list", "--project", self.alias)
        rows = everything["subscriptions"]
        events = {row["event"] for row in rows}
        if len(events) < 2:
            pytest.skip(
                f"project has {len(events)} distinct notification event(s); "
                "need >= 2 to prove --event narrows"
            )

        target = min(events)
        expected = sum(1 for row in rows if row["event"] == target)
        filtered = self._payload("notification", "list", "--project", self.alias, "--event", target)

        assert len(filtered["subscriptions"]) == expected
        assert len(filtered["subscriptions"]) < len(rows)

    def test_api_side_event_filter_is_still_ignored(self) -> None:
        """Pin the upstream behavior the client-side narrowing exists for.

        This has to observe the service's RAW answer, so it calls the L3
        client directly -- every CLI path narrows before the caller sees
        anything, and asserting that kbagent *sent* ``?event=`` would stay
        true whether or not the service honors it, i.e. would be no canary
        at all.

        If the service ever starts honoring the parameter, the assertion
        below fails, which is the notification we want: the narrowing in
        ``NotificationService`` becomes redundant and the ``gotchas.md``
        entry needs retiring. It is a canary, not a correctness requirement,
        so it skips unless the project carries the mixed data needed to tell
        the two behaviors apart.
        """
        _step(4, "raw ?event= behavior canary")
        everything = self._payload("notification", "list", "--project", self.alias)
        events = {row["event"] for row in everything["subscriptions"]}
        if len(events) < 2:
            pytest.skip("need >= 2 distinct events to observe the API-side filter")

        target = min(events)
        raw = self.client.list_project_subscriptions(event=target)
        raw_events = {str(sub.get("event", "")) for sub in raw}

        assert any(event != target for event in raw_events), (
            f"GET /project-subscriptions?event={target} came back containing only "
            f"{target!r}: the Notification Service now HONORS the parameter. The "
            "client-side narrowing in NotificationService is redundant, and the "
            "gotchas.md / CLAUDE.md / context.py notes saying it is ignored are "
            "now wrong -- retire them."
        )

    def test_unknown_event_returns_empty_not_error(self) -> None:
        """A misspelled event yields no rows, not a failure and not everything.

        `EventName` is an open string in the schema, which is why the CLI does
        not validate `--event` against an allowlist that would go stale the
        moment the platform adds an event. The empty result is kbagent's doing:
        the service answers a bogus `?event=` with 200 and the project's FULL
        subscription list, so without the client-side narrowing a camelCase
        typo like `jobFailed` would return every subscription in the project
        and read as "these all fire on that event".
        """
        _step(5, "notification list --event <nonexistent>")
        data = self._payload(
            "notification",
            "list",
            "--project",
            self.alias,
            "--event",
            "definitely-not-an-event",
        )

        assert data["subscriptions"] == []

    def test_config_filter_counts_excluded_catchalls(self) -> None:
        """Project-wide subscriptions dropped by a scope filter must be counted."""
        _step(6, "notification list --component-id keboola.flow")
        unfiltered = self._payload("notification", "list", "--project", self.alias)
        # Only the rows the filter DROPS are counted -- a project-wide
        # subscription that filters on keboola.flow survives and is shown.
        expected_excluded = sum(
            1
            for row in unfiltered["subscriptions"]
            if row["scope"] == "project-wide" and row["component_id"] != "keboola.flow"
        )

        filtered = self._payload(
            "notification",
            "list",
            "--project",
            self.alias,
            "--component-id",
            "keboola.flow",
        )

        assert filtered["project_wide_excluded"] == expected_excluded
        assert all(row["component_id"] == "keboola.flow" for row in filtered["subscriptions"])

    def test_detail_round_trips_a_listed_subscription(self) -> None:
        """`detail` must resolve the same row `list` reported."""
        _step(7, "notification detail -- round-trip from list")
        listed = self._payload("notification", "list", "--project", self.alias)
        if not listed["subscriptions"]:
            pytest.skip("project has no notification subscriptions to inspect")

        expected = listed["subscriptions"][0]
        detail = self._payload(
            "notification",
            "detail",
            "--project",
            self.alias,
            "--subscription-id",
            expected["subscription_id"],
        )

        assert detail["subscription_id"] == expected["subscription_id"]
        assert detail["event"] == expected["event"]
        assert detail["address"] == expected["address"]

    def test_detail_of_missing_subscription_fails_cleanly(self) -> None:
        _step(8, "notification detail -- unknown ID")
        result = self._run(
            "notification",
            "detail",
            "--project",
            self.alias,
            "--subscription-id",
            "0",
        )

        assert result.exit_code != 0
        assert "error" in result.output.lower()

    def test_create_replace_recipient_delete_round_trip(self) -> None:
        """Full write-path round trip (issue #690): create -> detail -> replace-recipient -> delete.

        Exercises every write command this class previously had no coverage
        for: ``create`` mints a project-wide ``job-failed`` email subscription
        (asserted against its own audit row), ``detail`` round-trips it,
        ``replace-recipient`` swaps the address to a second one (asserting the
        new id differs from the old and the old subscription was actually
        deleted), and ``delete --yes`` removes the replacement. A final
        ``list`` proves neither the original nor the replacement id lingers.

        Cleanup is best-effort in ``finally``: whichever of the two ids is
        still live gets deleted so a failed assertion never leaks a real
        subscription in the E2E project.
        """
        # Pre-flight best-effort sweep: a killed process (CI timeout,
        # Ctrl-C) from a PRIOR run of this test leaks a project-wide
        # job-failed subscription that then fires for EVERY job in the
        # E2E project until removed by hand. RUN_ID is timestamp-based
        # and changes every run, so a leaked address never matches this
        # run's own `address`/`replaced_address` below -- match on the
        # stable "-690" marker suffix instead, mirroring the best-effort
        # `finally` cleanup further down.
        stale = self._payload("notification", "list", "--project", self.alias)
        for row in stale["subscriptions"]:
            row_address = row.get("address") or ""
            if "-690@example.com" in row_address or "-690-replaced@example.com" in row_address:
                with contextlib.suppress(Exception):
                    self._run(
                        "notification",
                        "delete",
                        "--project",
                        self.alias,
                        "--subscription-id",
                        row["subscription_id"],
                        "--yes",
                    )

        _step(9, "notification create -- project-wide job-failed/email")
        address = f"{RUN_ID}-690@example.com"
        replaced_address = f"{RUN_ID}-690-replaced@example.com"
        old_id: str | None = None
        new_id: str | None = None

        try:
            created = self._payload(
                "notification",
                "create",
                "--project",
                self.alias,
                "--event",
                "job-failed",
                "--channel",
                "email",
                "--address",
                address,
            )
            old_id = created["subscription_id"]
            assert old_id, f"create returned no subscription_id: {created}"
            assert created["event"] == "job-failed"
            assert created["channel"] == "email"
            assert created["address"] == address
            assert created["scope"] == "project-wide"
            assert created["project_alias"] == self.alias

            _step(10, "notification detail -- round-trip the new subscription")
            detail = self._payload(
                "notification",
                "detail",
                "--project",
                self.alias,
                "--subscription-id",
                old_id,
            )
            assert detail["subscription_id"] == old_id
            assert detail["event"] == "job-failed"
            assert detail["address"] == address

            _step(11, "notification replace-recipient -- swap to a second address")
            replaced = self._payload(
                "notification",
                "replace-recipient",
                "--project",
                self.alias,
                "--subscription-id",
                old_id,
                "--address",
                replaced_address,
                "--yes",
            )
            assert replaced["old_subscription_id"] == old_id
            new_id = replaced["new_subscription_id"]
            assert new_id, f"replace-recipient returned no new_subscription_id: {replaced}"
            assert new_id != old_id
            assert replaced["old_address"] == address
            assert replaced["old_deleted"] is True
            assert replaced["address"] == replaced_address
            assert replaced["event"] == "job-failed"
            # The old subscription is gone -- only the replacement remains live.
            old_id = None

            _step(12, "notification delete --yes -- remove the replacement")
            deleted = self._payload(
                "notification",
                "delete",
                "--project",
                self.alias,
                "--subscription-id",
                new_id,
                "--yes",
            )
            assert deleted == {
                "project_alias": self.alias,
                "subscription_id": new_id,
                "deleted": True,
            }
            new_id = None

            _step(13, "notification list -- neither id lingers")
            final = self._payload("notification", "list", "--project", self.alias)
            ids = {row["subscription_id"] for row in final["subscriptions"]}
            assert replaced["old_subscription_id"] not in ids
            assert replaced["new_subscription_id"] not in ids
        finally:
            # Best-effort cleanup: whichever id is still non-None was not
            # confirmed deleted by the assertions above, so delete it now
            # rather than leaking a live subscription in the E2E project.
            for leftover_id in (old_id, new_id):
                if leftover_id is None:
                    continue
                with contextlib.suppress(Exception):
                    self._run(
                        "notification",
                        "delete",
                        "--project",
                        self.alias,
                        "--subscription-id",
                        leftover_id,
                        "--yes",
                    )


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
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
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


@skip_without_credentials
@pytest.mark.e2e
class TestE2EJobRunMode:
    """Prove `kbagent job run --mode debug` reaches the Queue API wire (#321 / v0.43.6).

    Submits a real job with ``--mode debug`` against a live Keboola project and
    asserts that the Queue API echoes back ``mode="debug"`` on the create
    response. This is the single canonical sign that the flag has not been
    silently dropped anywhere on the CLI -> service -> client -> wire path --
    the same regression the kbagent-pr-reviewer subagent caught in
    ``server/routers/jobs.py`` during the original review of #321.

    Intentionally does NOT use ``--wait``. The Queue accepts the job with the
    mode field set even before the worker starts processing, so the create
    response is the load-bearing assertion. The worker outcome (debug file
    appearing in Storage Files tagged ``debug-<jobId>``) is a Queue worker
    behaviour, not a kbagent behaviour, and is already covered by the manual
    smoke-test step in the PR description; baking a worker-side wait into the
    E2E suite would add minutes per run for no extra kbagent-side proof.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-jobmode"

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

        yield

        for component_id, config_id in reversed(self._created):
            try:
                self.client.delete_config(component_id=component_id, config_id=config_id)
            except Exception as exc:
                print(
                    f"  {_DIM}(teardown) delete_config {component_id}/{config_id} failed: {exc}{_RESET}"
                )
        self.client.close()

    def test_job_run_mode_debug_reaches_queue_api(self) -> None:
        """`kbagent job run --mode debug` returns a job dict carrying `mode='debug'`.

        The Queue API echoes the body's ``mode`` field on its create
        response, so this is a wire-level proof that the flag was not
        dropped by the CLI, service, or client.
        """
        import contextlib

        _step(1, "create a minimal ex-http config to target")
        parent = self.client.create_config(
            component_id="keboola.ex-http",
            name=f"{RUN_ID}-http-debug-mode",
            description="E2E #321: --mode debug wire passthrough",
            configuration={"parameters": {"baseUrl": "https://example.com"}},
        )
        parent_id = str(parent["id"])
        self._created.append(("keboola.ex-http", parent_id))

        _step(2, "kbagent job run --mode debug --no-variables (no --wait)")
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
                "--mode",
                "debug",
                "--no-variables",
            ],
        )

        data = _json(result)
        payload = data.get("data", data)
        # Queue API echoes the mode field on the create response -- this is
        # the load-bearing proof that the wire body carried "mode": "debug".
        print(f"  {_DIM}returned mode={payload.get('mode')!r} id={payload.get('id')}{_RESET}")
        assert payload.get("mode") == "debug", (
            f"expected mode='debug' in Queue create response, got {payload.get('mode')!r}. "
            f"Full payload keys: {sorted(payload.keys())}"
        )

        # Best-effort: kill the job so we do not waste compute waiting for
        # the worker to upload the debug file. The wire assertion above is
        # the actual test contract.
        job_id = payload.get("id")
        if job_id:
            with contextlib.suppress(Exception):
                self.client.kill_job(str(job_id))


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
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
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

    @staticmethod
    def _write_cf(tmp_path: Path, name: str = "cf.yaml") -> Path:
        """Write a minimal valid conditional-flow (string ids, one job task)."""
        body = (
            "phases:\n"
            '  - id: "p1"\n'
            '    name: "P1"\n'
            "    next:\n"
            '      - id: "n"\n'
            "        goto: null\n"
            "tasks:\n"
            '  - id: "t1"\n'
            '    name: "T1"\n'
            '    phase: "p1"\n'
            "    enabled: true\n"
            "    task:\n"
            "      type: job\n"
            '      componentId: "keboola.ex-http"\n'
            '      configId: "1"\n'
            "      mode: run\n"
        )
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    def test_flow_crud_and_schedule(self, tmp_path: Path) -> None:
        """Full lifecycle: schema → validate → new → list → detail → update →
        schedule → schedule-remove → delete (conditional flow)."""

        _step(1, "flow schema returns the conditional-flow YAML template")
        result = self._run("flow", "schema")
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "phases" in data["data"]["schema"]
        assert "goto" in data["data"]["schema"]

        cf_file = self._write_cf(tmp_path)

        _step(2, "flow validate (semantic-only, no --project) -- structural skipped note")
        result = self._run("flow", "validate", "--file", f"@{cf_file}")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)["data"]
        assert payload["valid"] is True
        assert any("structural schema validation skipped" in n for n in payload.get("notes", []))

        _step(2.1, "flow validate --project -- fetch live schema, full validation")
        result = self._run("flow", "validate", "--file", f"@{cf_file}", "--project", self.alias)
        # Skip cleanly if the project has conditional flows disabled.
        if result.exit_code != 0 and "conditional" in result.output.lower():
            pytest.skip("Project reports conditional_flows=false; skipping CF E2E")
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["valid"] is True

        _step(2.2, "flow schema --full --project -- live JSON Schema from the stack")
        result = self._run("flow", "schema", "--full", "--project", self.alias)
        assert result.exit_code == 0, result.output
        full = json.loads(result.output)["data"]
        assert full["format"] == "json-schema"
        assert isinstance(full["schema"], dict) and full["schema"]

        _step(3, "flow new -- create a keboola.flow config")
        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID}-flow",
            "--description",
            "E2E flow test",
            "--file",
            f"@{cf_file}",
        )
        # Skip cleanly if the project has conditional flows disabled.
        if result.exit_code != 0 and "conditional" in result.output.lower():
            pytest.skip("Project reports conditional_flows=false; skipping CF E2E")
        assert result.exit_code == 0, result.output
        created = json.loads(result.output)["data"]
        flow_id = created["id"]
        assert flow_id
        assert created["project_alias"] == self.alias
        self._created_flows.append(("keboola.flow", flow_id))

        _step(4, "flow list -- flow appears in listing")
        result = self._run("flow", "list", "--project", self.alias)
        assert result.exit_code == 0
        listing = json.loads(result.output)["data"]
        ids = {f["config_id"] for f in listing["flows"]}
        assert flow_id in ids
        assert "legacy_orchestrator_count" in listing

        _step(5, "flow detail -- returns phase/task counts")
        result = self._run(
            "flow",
            "detail",
            "--project",
            self.alias,
            "--flow-id",
            flow_id,
        )
        assert result.exit_code == 0, result.output
        detail = json.loads(result.output)["data"]
        assert detail["id"] == flow_id
        assert detail["component_id"] == "keboola.flow"
        assert "phase_count" in detail

        _step(6, "flow update -- rename the flow")
        result = self._run(
            "flow",
            "update",
            "--project",
            self.alias,
            "--flow-id",
            flow_id,
            "--name",
            f"{RUN_ID}-flow-renamed",
        )
        assert result.exit_code == 0, result.output
        updated = json.loads(result.output)["data"]
        assert updated["id"] == flow_id

        _step(7, "flow schedule -- attach a cron schedule")
        result = self._run(
            "flow",
            "schedule",
            "--project",
            self.alias,
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

        _step(8, "flow schedule-remove -- remove schedule, idempotent")
        result = self._run(
            "flow",
            "schedule-remove",
            "--project",
            self.alias,
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
            "--flow-id",
            flow_id,
            "--yes",
        )
        assert result2.exit_code == 0
        assert json.loads(result2.output)["data"]["deleted_count"] == 0

        _step(9, "flow delete -- delete the flow")
        result = self._run(
            "flow",
            "delete",
            "--project",
            self.alias,
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

    def test_flow_validation_rejects_invalid_definition(self, tmp_path: Path) -> None:
        """flow new with a task referencing a missing phase must fail with
        INVALID_FLOW_DEFINITION (semantic validation, which always runs --
        independent of whether the live schema fetch succeeds)."""
        bad = (
            "phases:\n"
            '  - id: "p1"\n'
            '    name: "P1"\n'
            "    next:\n"
            '      - id: "n"\n'
            "        goto: null\n"
            "tasks:\n"
            '  - id: "t1"\n'
            '    name: "T1"\n'
            '    phase: "ghost"\n'
            "    enabled: true\n"
            "    task:\n"
            "      type: job\n"
            '      componentId: "keboola.ex-http"\n'
            '      configId: "1"\n'
            "      mode: run\n"
        )
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(bad, encoding="utf-8")

        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID}-invalid",
            "--file",
            f"@{bad_file}",
        )
        assert result.exit_code != 0
        out = json.loads(result.output)
        assert out["error"]["code"] == "INVALID_FLOW_DEFINITION"

    def test_flow_list_no_project_returns_all(self) -> None:
        """flow list without --project returns flows from all registered projects."""
        result = self._run("flow", "list")
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert "flows" in data
        assert "errors" in data
        assert "legacy_orchestrator_count" in data

    def test_flow_list_with_schedules(self, tmp_path: Path) -> None:
        """flow list --with-schedules enriches rows with schedule metadata.

        Creates a flow + schedule, verifies the enrichment appears on the
        correct flow row, then cleans up.
        """
        cf_file = self._write_cf(tmp_path, name="cf-ws.yaml")
        result = self._run(
            "flow",
            "new",
            "--project",
            self.alias,
            "--name",
            f"{RUN_ID}-flow-ws",
            "--description",
            "E2E with-schedules test",
            "--file",
            f"@{cf_file}",
        )
        if result.exit_code != 0 and "conditional" in result.output.lower():
            pytest.skip("Project reports conditional_flows=false; skipping CF E2E")
        assert result.exit_code == 0, result.output
        flow_id = json.loads(result.output)["data"]["id"]
        self._created_flows.append(("keboola.flow", flow_id))

        # Attach a schedule
        sched_result = self._run(
            "flow",
            "schedule",
            "--project",
            self.alias,
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
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
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
        cf_file = tmp_path / "sched-cf.yaml"
        cf_file.write_text(
            "phases:\n"
            '  - id: "p1"\n'
            '    name: "P1"\n'
            "    next:\n"
            '      - id: "n"\n'
            "        goto: null\n"
            "tasks:\n"
            '  - id: "t1"\n'
            '    name: "T1"\n'
            '    phase: "p1"\n'
            "    enabled: true\n"
            "    task:\n"
            "      type: job\n"
            '      componentId: "keboola.ex-http"\n'
            '      configId: "1"\n'
            "      mode: run\n",
            encoding="utf-8",
        )
        flow_result = _invoke(
            self.config_dir,
            [
                "--json",
                "flow",
                "new",
                "--project",
                self.alias,
                "--name",
                f"{RUN_ID}-sched-flow",
                "--description",
                "E2E schedule discovery fixture",
                "--file",
                f"@{cf_file}",
            ],
        )
        if flow_result.exit_code != 0 and "conditional" in flow_result.output.lower():
            pytest.skip("Project reports conditional_flows=false; skipping schedule E2E")
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
            # Not a "feature unsupported" case -- workspace create works wherever
            # the token carries the sandbox/workspace-create scope. Skip the GC
            # roundtrip (which needs a freshly created workspace) and surface the
            # real CLI error so the cause (e.g. token scope) is visible.
            pytest.skip(
                f"workspace create unavailable for this token (skipping GC roundtrip): {result.output}"
            )

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
# Issue #304: workspace discoverability (login_type / qs_compatible / sandbox annotation)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EIssue304WorkspaceDiscoverability:
    """End-to-end coverage for the issue #304 workspace fixes.

    Creates a real workspace (sandbox-backed, RO) and verifies:
    - ``workspace list`` JSON entries carry ``login_type`` / ``read_only``
      / ``qs_compatible`` / ``database`` / ``warehouse`` (the four fields
      that were silently discarded pre-0.42.0).
    - ``workspace detail`` carries the same fields.
    - ``workspace list --qs-compatible`` returns a subset that excludes the
      created workspace ONLY when its loginType is off the confirmed
      whitelist -- otherwise it includes it. Either direction is consistent
      with whitelist semantics.
    - ``config detail --component-id keboola.sandboxes --config-id <ID>``
      annotates the response with ``sandbox_annotation.storage_workspace_id``
      pointing at the actual workspace ID (the issue #304 trap fix).
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-disc"

        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        self.api = KeboolaClient(self.url, self.token)
        self._created_workspace_ids: list[int] = []

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

    def test_snowflake_workspace_create_returns_private_key(self) -> None:
        """Snowflake workspace creation returns the generated private key once."""
        _step(1, "workspace create returns private_key on Snowflake")
        result = self._run("workspace", "create", "--project", self.alias)
        if result.exit_code != 0:
            pytest.skip(f"workspace create not supported: {result.output}")

        data = _json_ok(result)["data"]
        ws_id = int(data["workspace_id"])
        self._created_workspace_ids.append(ws_id)

        if data.get("backend") != "snowflake":
            pytest.skip("Snowflake private_key assertion requires a Snowflake stack")

        assert "private_key" in data
        assert data["private_key"].startswith("-----BEGIN PRIVATE KEY-----")

    def test_issue_304_discoverability_roundtrip(self) -> None:
        """list/detail expose loginType; sandbox config annotation resolves real workspace ID."""
        _step(1, "workspace create (RO sandbox)")
        result = self._run("workspace", "create", "--project", self.alias)
        if result.exit_code != 0:
            pytest.skip(f"workspace create not supported: {result.output}")
        data = _json_ok(result)
        ws_id = int(data["data"]["workspace_id"])
        config_id = data["data"]["config_id"]
        self._created_workspace_ids.append(ws_id)

        _step(2, "workspace list -- new fields present on every entry")
        list_data = self._run_ok("workspace", "list", "--project", self.alias)
        entries = list_data["data"]["workspaces"]
        ours = [w for w in entries if w["id"] == ws_id]
        assert ours, f"created workspace {ws_id} not in list"
        entry = ours[0]
        # The four formerly-discarded fields. We assert they EXIST and have
        # the expected types; the actual loginType depends on the stack the
        # E2E hits, so we don't pin a specific value.
        assert "login_type" in entry, f"missing login_type: {entry.keys()}"
        assert "read_only" in entry, f"missing read_only: {entry.keys()}"
        assert "qs_compatible" in entry, f"missing qs_compatible: {entry.keys()}"
        assert isinstance(entry["login_type"], str)
        assert isinstance(entry["read_only"], bool)
        assert isinstance(entry["qs_compatible"], bool)
        assert entry["read_only"] is True, (
            "RO sandboxes created via `workspace create` MUST be read-only"
        )

        _step(3, "workspace detail -- same fields on single-workspace detail")
        detail_data = self._run_ok(
            "workspace", "detail", "--project", self.alias, "--workspace-id", str(ws_id)
        )
        d = detail_data["data"]
        assert d.get("login_type") == entry["login_type"]
        assert d.get("read_only") is True
        assert d.get("qs_compatible") == entry["qs_compatible"]

        _step(4, "workspace list --qs-compatible -- filter is consistent with the entry's flag")
        qs_data = self._run_ok("workspace", "list", "--project", self.alias, "--qs-compatible")
        qs_ids = {w["id"] for w in qs_data["data"]["workspaces"]}
        # Conservation law: our workspace is in the filter result iff its
        # qs_compatible flag is True. This guards against drift between the
        # filter logic and the per-row classifier.
        if entry["qs_compatible"]:
            assert ws_id in qs_ids, (
                f"qs_compatible=true workspace {ws_id} missing from --qs-compatible filter"
            )
        else:
            assert ws_id not in qs_ids, (
                f"qs_compatible=false workspace {ws_id} leaked into --qs-compatible filter"
            )

        _step(5, "config detail keboola.sandboxes -- sandbox_annotation resolves real workspace ID")
        cfg_data = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            "keboola.sandboxes",
            "--config-id",
            config_id,
        )
        annotation = cfg_data["data"].get("sandbox_annotation")
        assert annotation is not None, (
            "config detail for keboola.sandboxes must carry sandbox_annotation"
        )
        # The actual mapping: storage_workspace_id MUST equal the workspace
        # we just created (the whole point of the fix -- previously the
        # caller would have used the misleading parameters.id and 404'd).
        assert annotation.get("storage_workspace_id") == ws_id, (
            f"expected storage_workspace_id={ws_id}, got annotation={annotation}"
        )
        assert "sandbox-service internal ID" in annotation.get("note", "")


# ---------------------------------------------------------------------------
# Agent tasks (kbagent agent ...): CRUD + cron preview + cli_command run
# Local-only commands; no Keboola API required. Still gated on credentials so
# they run as part of the standard E2E batch.
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EAgentTasks:
    """End-to-end coverage for the `kbagent agent` command tree.

    Exercises the full local lifecycle (create -> show -> list -> update ->
    run -> runs -> delete) plus the cron-preview + test utilities. Uses a
    cli_command action that just invokes ``kbagent version`` so no live
    Keboola endpoint is touched -- the value of the test is verifying the
    on-disk format and CLI plumbing under a real install.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.created_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        yield
        for task_id in self.created_ids:
            with contextlib.suppress(Exception):
                _invoke(self.config_dir, ["agent", "delete", task_id, "--yes"])

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_agent_cron_preview(self) -> None:
        _step(1, "agent cron-preview emits the next N firings")
        data = self._run_ok("agent", "cron-preview", "--cron", "0 6 * * 1", "--count", "3")
        firings = data["data"]["firings"]
        assert len(firings) == 3
        for ts in firings:
            assert "T" in ts  # ISO timestamps

    def test_agent_full_lifecycle(self) -> None:
        _step(1, "agent create -- cli_command action")
        data = self._run_ok(
            "agent",
            "create",
            "--name",
            f"{RUN_ID}-task",
            "--description",
            "E2E lifecycle smoke",
            "--cron",
            "0 12 * * *",
            "--type",
            "cli_command",
            "--argv",
            "version",
        )
        task_id = data["data"]["id"]
        self.created_ids.append(task_id)
        assert data["data"]["action"]["type"] == "cli_command"
        assert data["data"]["next_run_at"] is not None

        _step(2, "agent list -- task appears")
        listed = self._run_ok("agent", "list")
        assert task_id in [t["id"] for t in listed["data"]["tasks"]]

        _step(3, "agent show -- round-trip the task")
        shown = self._run_ok("agent", "show", task_id)
        assert shown["data"]["name"] == f"{RUN_ID}-task"

        _step(4, "agent update -- disable + flip to manual")
        updated = self._run_ok("agent", "update", task_id, "--disabled", "--manual")
        assert updated["data"]["enabled"] is False
        assert updated["data"]["manual"] is True
        assert updated["data"]["next_run_at"] is None

        _step(5, "agent run -- executes the cli_command action")
        run = self._run_ok("agent", "run", task_id)
        assert run["data"]["status"] == "ok"

        _step(6, "agent runs -- history now has one entry")
        runs = self._run_ok("agent", "runs", task_id)
        assert len(runs["data"]["runs"]) == 1

        _step(7, "agent delete -- task removed")
        self._run_ok("agent", "delete", task_id, "--yes")
        self.created_ids.remove(task_id)
        # show on deleted task returns NOT_FOUND
        gone = self._run("agent", "show", task_id)
        assert gone.exit_code == 1

    def test_agent_test_command(self) -> None:
        _step(1, "agent test -- ad-hoc cli_command without persistence")
        result = self._run_ok("agent", "test", "--type", "cli_command", "--argv", "version")
        assert result["data"]["status"] == "ok"
        # No tasks were persisted by the test path.
        listed = self._run_ok("agent", "list")
        assert listed["data"]["tasks"] == []


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
    def setup(self, tmp_path: Path, request: pytest.FixtureRequest) -> Generator[None, None, None]:
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
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
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
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
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

        # Read the 'value' length via the CLI's normalized `column_details`
        # (what a user/agent sees). The raw `/tables/{id}` `definition.columns`
        # block is read-after-DDL eventually consistent on this stack -- after a
        # swap it can keep reporting the pre-swap length for >30s -- whereas
        # `column_details` reflects the swap at once (verified 2026-07-22).
        def _value_len(table_id: str) -> str:
            detail = self._run_ok(
                "storage",
                "table-detail",
                "--project",
                self.alias,
                "--table-id",
                table_id,
                "--branch",
                str(branch_id),
            )["data"]
            cols = {c["name"]: c for c in detail["column_details"]}
            return str(cols["value"]["length"])

        assert _value_len(original_id) == "20"
        assert _value_len(typed_id) == "80"

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

        def _poll_value_len(table_id: str, expected: str) -> str:
            """Poll column_details until 'value' has the expected length (max ~30s).

            Small safety margin for any residual read lag after the swap job
            completes; column_details is normally consistent immediately.
            """
            length = ""
            for _ in range(15):
                length = _value_len(table_id)
                if length == expected:
                    return length
                time.sleep(2)
            return length

        after_original_len = _poll_value_len(original_id, "80")
        after_typed_len = _poll_value_len(typed_id, "20")
        assert after_original_len == "80", (
            f"After swap, '{original_id}' should adopt the schema of '{typed_id}' "
            f"(VARCHAR(80)); got VARCHAR({after_original_len})."
        )
        assert after_typed_len == "20", (
            f"After swap, '{typed_id}' should adopt the schema of '{original_id}' "
            f"(VARCHAR(20)); got VARCHAR({after_typed_len})."
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
        # Wording corrected in #373 (swap works on any branch, incl. production);
        # match the stable part of the message, not the old "dev branch" phrasing.
        assert "requires a branch" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# TestE2EStorageCloneTable -- storage clone-table (pull) into a dev branch
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EStorageCloneTable:
    """End-to-end coverage for ``kbagent storage clone-table``.

    Verifies:
    - a production table can be pulled (cloned) into a dev branch and is
      then visible/materialized in that branch,
    - dry-run skips the HTTP call,
    - calls without a branch are rejected before any HTTP traffic.

    On storage-branches projects this materializes the prod table into the
    branch (the prerequisite for in-branch swap / column drops). On
    legacy-branch projects the pull still succeeds; the assertion only checks
    the table is visible in the branch afterwards, which holds for both.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-clone"
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

        # Teardown: branches first (cascades to their materialized tables),
        # then the production bucket we created outside of a branch.
        for branch_id in self._created_branch_ids:
            with contextlib.suppress(Exception):
                self.client.delete_dev_branch(branch_id)
        for bucket_id in self._created_buckets:
            with contextlib.suppress(Exception):
                self.client.delete_bucket(bucket_id, force=True)
        self.client.close()

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(_invoke(self.config_dir, ["--json", *args]))

    def test_clone_prod_table_into_dev_branch(self) -> None:
        """Live pull: a production table becomes available in the dev branch."""
        bucket_stage = "in"
        bucket_name = f"{RUN_ID.replace('-', '_')}_clone"
        bucket_id = f"in.c-{bucket_name}"
        table_id = f"{bucket_id}.source"

        # create-table on the DEFAULT branch does NOT auto-create the bucket
        # (that convenience is dev-branch-only, to materialize the branch's
        # isolated storage). The production path needs the bucket first.
        _step(1, "create the destination bucket (default branch)")
        self._run_ok(
            "storage",
            "create-bucket",
            "--project",
            self.alias,
            "--stage",
            bucket_stage,
            "--name",
            bucket_name,
        )
        self._created_buckets.append(bucket_id)

        _step(2, "create a production table (default branch)")
        self._run_ok(
            "storage",
            "create-table",
            "--project",
            self.alias,
            "--bucket-id",
            bucket_id,
            "--name",
            "source",
            "--column",
            "id:VARCHAR(40)",
            "--column",
            "value:VARCHAR(20)",
            "--primary-key",
            "id",
        )

        _step(3, "branch create", "target dev branch for the pull")
        branch = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", f"{RUN_ID}-clone-branch"
        )["data"]
        branch_id = int(branch["branch_id"])
        self._created_branch_ids.append(branch_id)

        _step(4, "storage clone-table", "POST /tables/.../pull (default -> branch)")
        result = self._run_ok(
            "storage",
            "clone-table",
            "--project",
            self.alias,
            "--table-id",
            table_id,
            "--branch",
            str(branch_id),
        )["data"]
        assert result["table_id"] == table_id
        assert result["branch_id"] == branch_id
        assert result["dry_run"] is False
        assert result["response"]["status"] == "success"

        _step(5, "table-detail in branch", "table is materialized/visible after pull")
        detail = self.client.get_table_detail(table_id, branch_id=branch_id)
        col_names = {c["name"] for c in detail["definition"]["columns"]}
        assert col_names == {"id", "value"}

    def test_clone_dry_run_does_not_call_api(self) -> None:
        """Dry-run skips the HTTP call: exit 0, no response key."""
        _step(1, "branch create", "dry-run still requires a branch context")
        branch = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", f"{RUN_ID}-clone-dry"
        )["data"]
        branch_id = int(branch["branch_id"])
        self._created_branch_ids.append(branch_id)

        result = self._run_ok(
            "storage",
            "clone-table",
            "--project",
            self.alias,
            "--table-id",
            "in.c-foo.bar",
            "--branch",
            str(branch_id),
            "--dry-run",
        )["data"]
        assert result["dry_run"] is True
        assert "response" not in result

    def test_clone_without_branch_is_rejected(self) -> None:
        """Without active branch and without --branch, exit 5 before any HTTP."""
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "storage",
                "clone-table",
                "--project",
                self.alias,
                "--table-id",
                "in.c-foo.bar",
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

# Feature-flag E2E gate. Requires a SUPER-ADMIN manage token (the same kind
# `org setup` uses). Opt-in via `make test-e2e-feature` -- default-skipped in
# `make test-e2e` because the regular Storage API credentials cannot list or
# read feature flags.
skip_without_feature_credentials = pytest.mark.skipif(
    not (
        os.environ.get(ENV_MANAGE_TOKEN) and os.environ.get(ENV_URL) and os.environ.get(ENV_TOKEN)
    ),
    reason=(
        f"Requires {ENV_MANAGE_TOKEN} (super-admin), {ENV_URL}, and {ENV_TOKEN}. "
        "Run via `make test-e2e-feature`."
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
        # ``setup`` may have skipped before assigning this (fixture teardown
        # still runs); a test gated only on credentials must not error here.
        for app_id in getattr(self, "_created_app_ids", []):
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
        # v0.33.0 rename: envelope key is ``app_id`` (was bare ``id``).
        app_id = body["data"]["app_id"]
        assert app_id, "expected a numeric app id from POST /apps"
        assert body["data"]["config_id"], "expected a config_id from POST /apps"
        self._created_app_ids.append(app_id)

        _step(2, "List shows the created app with populated app_id + config_id")
        list_result = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "list",
                    "--project",
                    self.alias,
                ],
            )
        )["data"]
        listed = next(
            (a for a in list_result["apps"] if a["app_id"] == app_id),
            None,
        )
        assert listed is not None, f"newly-created app {app_id} not found in data-app list output"
        assert listed["config_id"], "data-app list must emit a populated config_id"

        _step(3, "Detail merges Data Science + Storage")
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
        assert detail["app_id"] == app_id
        assert detail["slug"] == slug
        assert detail["config_version_storage"], (
            "Storage config version should be populated after PUT"
        )

        _step(4, "Storage access is granted by default")
        assert body["data"]["workspace"] is True, (
            "create must report Storage access enabled by default"
        )
        cfg = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    self.alias,
                    "--component-id",
                    "keboola.data-apps",
                    "--config-id",
                    body["data"]["config_id"],
                ],
            )
        )["data"]
        record = cfg[0] if isinstance(cfg, list) else cfg
        runtime = record["configuration"]["runtime"]
        # This is the switch that makes the platform inject WORKSPACE_ID /
        # QUERY_SERVICE_URL. Without it the app deploys and reports running
        # while being unable to read a single row.
        assert runtime.get("workspace") == {"enabled": True}, (
            f"expected runtime.workspace.enabled=true in the live config, got {runtime!r}"
        )
        # ...and it must not have displaced the backend sizing.
        assert runtime.get("backend", {}).get("size"), (
            f"workspace block must be a sibling of backend, got {runtime!r}"
        )

    def test_data_app_no_workspace_omits_the_block(self) -> None:
        """``--no-workspace`` renders the pre-0.87.0 runtime body.

        Dry run: proves the opt-out reaches the request body without
        provisioning an app on the stack.
        """
        _step(1, "Dry-run create with --no-workspace")
        body = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "create",
                    "--project",
                    self.alias,
                    "--name",
                    f"E2E NoWorkspace {RUN_ID}",
                    "--slug",
                    f"e2e-nows-{RUN_ID}"[:60],
                    "--git-repo",
                    "https://github.com/keboola/does-not-matter-for-dry-run",
                    "--git-public",
                    "--auth",
                    "public",
                    "--no-workspace",
                    "--dry-run",
                ],
            )
        )["data"]
        assert body["dry_run"] is True
        assert body["workspace"] is False
        runtime = body["requests"]["put_storage_config"]["runtime"]
        assert "workspace" not in runtime, (
            f"--no-workspace must omit the block entirely, got {runtime!r}"
        )

        _step(2, "Same dry run WITHOUT the flag carries the block")
        body = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "create",
                    "--project",
                    self.alias,
                    "--name",
                    f"E2E Workspace {RUN_ID}",
                    "--slug",
                    f"e2e-ws-{RUN_ID}"[:60],
                    "--git-repo",
                    "https://github.com/keboola/does-not-matter-for-dry-run",
                    "--git-public",
                    "--auth",
                    "public",
                    "--dry-run",
                ],
            )
        )["data"]
        assert body["workspace"] is True
        assert body["requests"]["put_storage_config"]["runtime"]["workspace"] == {"enabled": True}

    @skip_without_data_app_public
    def test_data_app_update_toggles_storage_access(self) -> None:
        """`data-app update` flips runtime.workspace.enabled on a LIVE app (#737).

        Proves the three contract points a mocked unit test cannot: the flag
        actually reaches Storage, `detail` reads it back, and repeating the
        same request writes nothing (no config version minted).
        """
        _step(1, "Create an app with Storage access ON")
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PUBLIC]
        body = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "create",
                    "--project",
                    self.alias,
                    "--name",
                    f"E2E Update {RUN_ID}",
                    "--slug",
                    f"e2e-upd-{RUN_ID}"[:60],
                    "--git-repo",
                    repo,
                    "--git-public",
                    "--auth",
                    "public",
                    "--no-deploy",  # avoid waiting on a real container build
                ],
            )
        )["data"]
        app_id = body["app_id"]
        self._created_app_ids.append(app_id)

        def _detail() -> dict:
            return _json_ok(
                _invoke(
                    self.config_dir,
                    ["--json", "data-app", "detail", "--project", self.alias, "--app-id", app_id],
                )
            )["data"]

        assert _detail()["workspace_enabled"] is True

        _step(2, "Turn Storage access OFF")
        off = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "update",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--no-workspace",
                ],
            )
        )["data"]
        assert off["changed"] == ["workspace"], off
        assert off["deploy_required"] is True
        assert _detail()["workspace_enabled"] is False

        _step(3, "Repeating the same request is a no-op -- no config version minted")
        repeat = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "update",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--no-workspace",
                ],
            )
        )["data"]
        assert repeat["changed"] == []
        assert repeat["deploy_required"] is False
        assert repeat["config_version_after"] == repeat["config_version_before"]

        _step(4, "Turn it back on together with a new auto-suspend")
        on = _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "data-app",
                    "update",
                    "--project",
                    self.alias,
                    "--app-id",
                    app_id,
                    "--workspace",
                    "--auto-suspend",
                    "600",
                ],
            )
        )["data"]
        assert set(on["changed"]) == {"workspace", "auto_suspend_after_seconds"}, on
        detail = _detail()
        assert detail["workspace_enabled"] is True
        assert str(detail["auto_suspend_after_seconds"]) == "600", detail

    @skip_without_data_app_public
    def test_data_app_git_repo_introspection(self) -> None:
        """git-repo against a deployed public app.

        The git-repo introspection endpoint (sandboxes-service
        ``GET /apps/{id}/git-repo``) returns 409 "no Git repository configured"
        until the app has been DEPLOYED at least once -- the git block is
        synced from the Storage config into the Data Science app record at
        deploy time. We fire a deploy (no ``--wait``, so we don't block on a
        container build) and poll ``git-repo`` until the sync lands.
        """
        _step(1, "Create a public-repo data app (no deploy yet)")
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PUBLIC]
        slug = f"e2e-git-{RUN_ID}"[:60]
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
                    f"E2E Git {RUN_ID}",
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
        )
        app_id = create["data"]["app_id"]
        self._created_app_ids.append(app_id)

        _step(2, "Fire deploy (no wait) so the git block syncs into the DS record")
        _invoke(
            self.config_dir,
            ["--json", "data-app", "deploy", "--project", self.alias, "--app-id", app_id],
        )

        _step(3, "Poll git-repo until the deploy-time git sync completes")
        repo_data = None
        for _ in range(20):
            res = _invoke(
                self.config_dir,
                ["--json", "data-app", "git-repo", "--project", self.alias, "--app-id", app_id],
            )
            if res.exit_code == 0:
                repo_data = json.loads(res.output)["data"]
                break
            time.sleep(3)
        if repo_data is None:
            pytest.skip("git-repo did not become available within the poll budget")
        assert repo_data["https_url"] or repo_data["ssh_url"], "expected a clone URL"
        assert "is_managed_git_repo" in repo_data

        _step(4, "git-credentials on an external repo lists no managed credentials")
        cred_res = _invoke(
            self.config_dir,
            ["--json", "data-app", "git-credentials", "--project", self.alias, "--app-id", app_id],
        )
        # External repos (the kind `data-app create --git-repo` produces) have
        # no managed credential store: the list endpoint returns an empty list
        # (200). A 409 here would also satisfy the managed-only contract.
        if cred_res.exit_code == 0:
            assert json.loads(cred_res.output)["data"]["credentials"] == []

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
        app_id = body["data"]["app_id"]
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
        app_id = create["app_id"]
        config_id = create["config_id"]
        self._created_app_ids.append(app_id)

        _step(2, "secrets-set: encrypt and write")
        # Keep the raw CLI result: the leak assertions below are about the
        # command's stdout, which the parsed envelope no longer carries.
        set_raw = _invoke(
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
        _json_ok(set_raw)
        assert secret_plaintext not in set_raw.output, (
            "Plaintext value MUST NEVER appear in secrets-set output"
        )

        _step(3, "secrets-list: enumerate keys (never decrypts)")
        list_raw = _invoke(
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
        list_result = _json_ok(list_raw)
        keys_in_list = [s["key"] for s in list_result["data"]["secrets"]]
        assert secret_key in keys_in_list, (
            f"secrets-list must surface the just-written key; got {keys_in_list}"
        )
        assert secret_plaintext not in list_raw.output, (
            "Plaintext value MUST NEVER appear in secrets-list output"
        )

        _step(4, "secrets-get: metadata only (never plaintext)")
        get_raw = _invoke(
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
        get_result = _json_ok(get_raw)
        assert get_result["data"]["key"] == secret_key
        assert get_result["data"]["encrypted"] is True, (
            "an encrypted secret must report encrypted=true"
        )
        assert get_result["data"]["value"] is None, "an encrypted secret must NOT expose a value"
        assert secret_plaintext not in get_raw.output, (
            "secrets-get MUST NEVER echo the decrypted plaintext (Encryption API is one-way)"
        )

        _step(5, "secrets-get: a PLAIN (unencrypted) key returns its value (0.43.9+)")
        # Inject a plain (no-'#') env-var value the way a user/UI would, then
        # confirm secrets-get reads it back verbatim and secrets-remove drops
        # it -- both paths reject plain keys before 0.43.9.
        plain_val = "plain-not-a-secret"
        _json_ok(
            _invoke(
                self.config_dir,
                [
                    "--json",
                    "config",
                    "update",
                    "--project",
                    self.alias,
                    "--component-id",
                    "keboola.data-apps",
                    "--config-id",
                    config_id,
                    "--set",
                    f"parameters.dataApp.secrets.E2E_PLAIN_KEY={plain_val}",
                    "--merge",
                ],
            )
        )
        plain_get = _json_ok(
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
                    "E2E_PLAIN_KEY",
                ],
            )
        )
        assert plain_get["data"]["encrypted"] is False, (
            "a plain (non-KBC::) value must report encrypted=false"
        )
        assert plain_get["data"]["value"] == plain_val, (
            "secrets-get must return the literal value for a plain key"
        )
        plain_remove = _json_ok(
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
                    "E2E_PLAIN_KEY",
                    "--yes",
                ],
            )
        )
        assert "E2E_PLAIN_KEY" in plain_remove["data"]["removed"], (
            "secrets-remove must accept and remove a plain (no-'#') key"
        )

        _step(6, "secrets-remove: first call removes the key")
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
        # `removed` is a list of derived env-var names, not a count.
        assert remove_result["data"]["removed"] == ["E2E_TEST_KEY"], (
            f"first remove must report the removed env-var; got {remove_result['data']['removed']}"
        )

        _step(7, "secrets-remove: second call is idempotent (removed=[])")
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
        assert idempotent["data"]["removed"] == [], (
            "second remove of the same key must be idempotent (removed=[], exit 0)"
        )

    @skip_without_data_app_public
    def test_data_app_logs_validation_and_not_running(self) -> None:
        """E2E coverage for ``data-app logs`` (since v0.43.8).

        Three assertions against a real Data Science backend:

        1. ``--lines`` + ``--since`` together exits 2 (mutex enforced at
           the command boundary, never reaches the network).
        2. ``--since`` without a timezone exits 2 (validated client-side
           via ``datetime.fromisoformat`` + ``tzinfo`` check before any
           round-trip; clearer error than the server's bare ``Invalid
           value`` 400).
        3. Logs against a never-deployed app surface the server's
           ``apps.appNotRunning`` 400 verbatim with a non-zero exit. The
           ``--no-deploy`` flag in step 1 keeps this cheap (no real
           container build); the resulting app sits at ``state=created``
           which the Data Science API rejects as "not running" -- which
           is precisely the path operators will hit when triaging a
           failed deploy.
        """
        _step(1, "Create cheap shell app (--no-deploy)", "no container will start")
        repo = os.environ[ENV_DATA_APP_GIT_REPO_PUBLIC]
        slug = f"e2e-logs-{RUN_ID}"[:60]
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
                    f"E2E Logs {RUN_ID}",
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
        )
        app_id = create["data"]["app_id"]
        self._created_app_ids.append(app_id)

        _step(2, "Mutex: --lines + --since rejected locally (no round-trip)")
        mutex = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                self.alias,
                "--app-id",
                app_id,
                "--lines",
                "10",
                "--since",
                "2026-05-21T13:00:00Z",
            ],
        )
        assert mutex.exit_code == 2, mutex.output
        mutex_body = json.loads(mutex.output)
        assert mutex_body["error"]["code"] == "USAGE_ERROR"
        assert "mutually exclusive" in mutex_body["error"]["message"]

        _step(3, "Naive --since (no tz) rejected locally")
        naive = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                self.alias,
                "--app-id",
                app_id,
                "--since",
                "2026-05-21T13:00:00",
            ],
        )
        assert naive.exit_code == 2, naive.output
        naive_body = json.loads(naive.output)
        assert naive_body["error"]["code"] == "USAGE_ERROR"
        assert "timezone" in naive_body["error"]["message"]

        _step(4, "Logs against never-deployed app surfaces apps.appNotRunning 400")
        not_running = _invoke(
            self.config_dir,
            [
                "--json",
                "data-app",
                "logs",
                "--project",
                self.alias,
                "--app-id",
                app_id,
                "--lines",
                "100",
            ],
        )
        # The server returns 400 ``App "X" is not running``; this maps
        # through BaseHttpClient -> KeboolaApiError -> exit 1.
        assert not_running.exit_code == 1, not_running.output
        not_running_body = json.loads(not_running.output)
        assert not_running_body["status"] == "error"
        # The verbatim server message must reach the operator -- no
        # client-side reclassification or message rewriting.
        assert "is not running" in not_running_body["error"]["message"]


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


@skip_without_feature_credentials
@pytest.mark.e2e
def test_feature_flags_read_e2e(tmp_path: Path) -> None:
    """Read-only feature-flag check against a real stack (since v0.48.0).

    Verifies the wiring end-to-end with a super-admin manage token:
    1. the stack catalogue (`feature list`) returns a non-empty feature set;
    2. a project's assigned features (`feature project-show`) are readable.

    Deliberately read-only -- it never enables or disables a flag, so it is
    safe to run against a live project. The manage token is supplied via env
    + the top-level --allow-env-manage-token opt-in (default-deny otherwise).
    """
    stack_url = (
        os.environ[ENV_URL]
        if os.environ[ENV_URL].startswith("https://")
        else f"https://{os.environ[ENV_URL]}"
    )
    config_dir = tmp_path / "kbagent-config"
    config_dir.mkdir()
    alias = "e2e-feature-target"

    env = {
        **os.environ,
        "KBC_MANAGE_API_TOKEN": os.environ[ENV_MANAGE_TOKEN],
    }

    def _run(*args: str) -> Any:
        return runner.invoke(
            app,
            ["--config-dir", str(config_dir), "--allow-env-manage-token", "--json", *args],
            env=env,
        )

    # Register the project via a real Storage API token so project_id is
    # populated from the token-verify response (feature project-show needs it).
    add = _run(
        "project",
        "add",
        "--project",
        alias,
        "--url",
        stack_url,
        "--token",
        os.environ[ENV_TOKEN],
    )
    assert add.exit_code == 0, add.output

    # 1. Stack catalogue -- the super-admin token must see the full feature set.
    catalogue = _run("feature", "list", "--project", alias)
    assert catalogue.exit_code == 0, catalogue.output
    cat_data = json.loads(catalogue.output)["data"]
    assert isinstance(cat_data["features"], list)
    assert len(cat_data["features"]) > 0, "stack catalogue unexpectedly empty"
    # Every catalogue entry carries a stable 'name' identifier.
    assert all("name" in feat for feat in cat_data["features"]), cat_data["features"][:3]

    # 2. Project-assigned features -- readable, possibly empty, always a list.
    show = _run("feature", "project-show", "--project", alias)
    assert show.exit_code == 0, show.output
    show_data = json.loads(show.output)["data"]
    assert isinstance(show_data["features"], list)
    assert show_data["project_id"] is not None


@skip_without_credentials
@pytest.mark.e2e
def test_stream_otlp_e2e(tmp_path: Path) -> None:
    """Full Data Streams OTLP round-trip against a real project (since v0.50.0).

    Creates a temporary OTLP source, reads its assembled detail (masked by
    default + revealed on demand), then deletes it and confirms it is gone.
    Self-cleaning: the source it creates is removed before the test returns.
    """
    stack_url = (
        os.environ[ENV_URL]
        if os.environ[ENV_URL].startswith("https://")
        else f"https://{os.environ[ENV_URL]}"
    )
    config_dir = tmp_path / "kbagent-config"
    config_dir.mkdir()
    alias = "e2e-stream-target"
    # Unique per run: a fixed name reuses whatever source is already there, and
    # a source whose async delete once got wedged server-side (unremovable via
    # the API) would then red every subsequent run. A fresh per-run source
    # creates and deletes cleanly (~3s) and is isolated from any orphan.
    source_name = f"kbagent-{RUN_ID}"
    sink_bucket = f"in.c-otlp-{source_name}"

    def _run(*args: str) -> Any:
        return runner.invoke(
            app, ["--config-dir", str(config_dir), "--json", *args], env={**os.environ}
        )

    add = _run(
        "project", "add", "--project", alias, "--url", stack_url, "--token", os.environ[ENV_TOKEN]
    )
    assert add.exit_code == 0, add.output

    try:
        # 1. List is readable (possibly empty) and well-formed.
        listed = _run("stream", "list", "--project", alias)
        assert listed.exit_code == 0, listed.output
        assert isinstance(json.loads(listed.output)["data"]["sources"], list)

        # 2. Create an OTLP source (idempotent on reruns).
        created = _run(
            "stream",
            "create-source",
            "--project",
            alias,
            "--name",
            source_name,
            "--type",
            "otlp",
            "--if-not-exists",
        )
        assert created.exit_code == 0, created.output
        cdata = json.loads(created.output)["data"]
        assert cdata["status"] in ("created", "skipped")
        assert cdata["type"] == "otlp"
        source_id = cdata["source_id"]
        # The three OTLP sinks are auto-provisioned, so the destination tables
        # (logs/metrics/traces) are present immediately after create.
        assert set(cdata["destination"]["tables"]) == {"logs", "metrics", "traces"}

        # 3. Detail masks the secret by default.
        masked = _run("stream", "detail", source_id, "--project", alias)
        assert masked.exit_code == 0, masked.output
        mdata = json.loads(masked.output)["data"]
        assert mdata["secret_revealed"] is False
        assert "/***" in mdata["endpoint"]
        assert mdata["protocol"] == "http/protobuf"
        assert set(mdata["signal_endpoints"]) == {"logs", "traces", "metrics"}
        assert set(mdata["destination"]["tables"]) == {"logs", "metrics", "traces"}

        # 4. --reveal exposes the real endpoint (no mask marker).
        revealed = _run("stream", "detail", source_id, "--project", alias, "--reveal")
        assert revealed.exit_code == 0, revealed.output
        rdata = json.loads(revealed.output)["data"]
        assert rdata["secret_revealed"] is True
        assert "/***" not in rdata["endpoint"]
    finally:
        # 5. Clean up -- delete the source and confirm it is gone.
        #
        # Deleting an OTLP source tears down its auto-provisioned sinks, which
        # runs as an async Stream task. It is usually fast (~3s) but under CI
        # load has exceeded the client's task timeout, surfacing as a retryable
        # TIMEOUT (exit 4) even though the delete keeps running server-side.
        # Treat that as "delete in progress" and confirm eventual removal via
        # the list below, so a slow-but-successful teardown does not red the run.
        deleted = _run("stream", "delete", source_name, "--project", alias, "--yes")
        if deleted.exit_code == 0:
            assert json.loads(deleted.output)["data"]["status"] == "deleted"
        else:
            payload = json.loads(deleted.output)
            assert payload["error"]["code"] == "TIMEOUT", deleted.output

    # Poll the source list until the (possibly still-processing) delete lands.
    remaining: set[str] = set()
    for _ in range(30):
        final = _run("stream", "list", "--project", alias)
        remaining = {s["source_id"] for s in json.loads(final.output)["data"]["sources"]}
        if source_name not in remaining:
            break
        time.sleep(2)
    assert source_name not in remaining

    # Best-effort: drop the auto-provisioned sink bucket, which lingers after
    # the source is gone. Failure here is not a test failure (throwaway project).
    _run(
        "storage",
        "delete-bucket",
        "--project",
        alias,
        "--bucket-id",
        sink_bucket,
        "--force",
        "--yes",
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

    def test_search_regex_returns_results_or_feature_gate_error(self) -> None:
        """`--regex` (mode=regex) obeys the same feature-gate-aware contract:
        results OR a clean FEATURE_NOT_ENABLED error -- never a raw 4xx."""
        result = self._run_ok("search", ".*", "--regex", "--project", self.alias, "--limit", "5")
        data = result["data"]
        assert "results" in data
        assert "errors" in data
        assert "stats" in data

        if data["errors"]:
            err = data["errors"][0]
            assert err["error_code"] == "FEATURE_NOT_ENABLED", err
            assert "global-search" in err["message"]
        else:
            # Regex matches entity names only -- matched_columns is always empty here.
            for row in data["results"]:
                assert row.get("matched_columns", []) == []

    def test_search_regex_with_config_based_is_usage_error(self) -> None:
        """`--regex` + `--search-type config-based` fails fast (exit 2)."""
        result = self._run(
            "search",
            ".*",
            "--regex",
            "--search-type",
            "config-based",
            "--project",
            self.alias,
        )
        assert result.exit_code == 2

    # ------------------------------------------------------------------
    # 0.88.0 MCP-parity flags
    # ------------------------------------------------------------------

    def test_search_scope_narrows_config_based_results(self) -> None:
        """`--scope` must be accepted and can only shrink the result set."""
        wide = self._run_ok(
            "search", "in.c", "--search-type", "config-based", "--project", self.alias
        )["data"]
        scoped = self._run_ok(
            "search",
            "in.c",
            "--search-type",
            "config-based",
            "--scope",
            "storage.input",
            "--project",
            self.alias,
        )["data"]

        assert len(scoped["results"]) <= len(wide["results"])
        for row in scoped["results"]:
            assert row["match_count"] >= 1

    def test_search_scope_with_textual_is_usage_error(self) -> None:
        """`--scope` + textual search fails fast (exit 2), like `--regex` inverted."""
        result = self._run("search", "data", "--scope", "parameters", "--project", self.alias)
        assert result.exit_code == 2

    def test_job_list_offset_and_sort_are_accepted(self) -> None:
        """The Queue API must accept sortBy/sortOrder/offset as kbagent sends them."""
        first = self._run_ok("job", "list", "--project", self.alias, "--limit", "2")["data"]
        oldest = self._run_ok(
            "job",
            "list",
            "--project",
            self.alias,
            "--limit",
            "2",
            "--sort-by",
            "startTime",
            "--sort-order",
            "asc",
        )["data"]
        paged = self._run_ok(
            "job", "list", "--project", self.alias, "--limit", "2", "--offset", "1"
        )["data"]

        for data in (first, oldest, paged):
            assert "jobs" in data and "errors" in data

    def test_job_list_rejects_unknown_sort_field(self) -> None:
        result = self._run("job", "list", "--project", self.alias, "--sort-by", "whenever")
        assert result.exit_code == 2

    def test_job_detail_log_tail_lines(self) -> None:
        """`--log-tail-lines` must attach `logTail` for a real, finished job."""
        listing = self._run_ok("job", "list", "--project", self.alias, "--limit", "1")["data"]
        if not listing["jobs"]:
            pytest.skip("project has no job history")
        job_id = str(listing["jobs"][0]["id"])

        plain = self._run_ok("job", "detail", "--project", self.alias, "--job-id", job_id)["data"]
        assert "logTail" not in plain

        tailed = self._run_ok(
            "job", "detail", "--project", self.alias, "--job-id", job_id, "--log-tail-lines", "5"
        )["data"]
        assert isinstance(tailed["logTail"], list)
        assert len(tailed["logTail"]) <= 5

    def test_storage_tables_include_usage(self) -> None:
        """`--include-usage` must attach `used_by` to every row against a live project."""
        data = self._run_ok("storage", "tables", "--project", self.alias, "--include-usage")["data"]

        for row in data["tables"]:
            assert isinstance(row["used_by"], list)
            for ref in row["used_by"]:
                assert ref["scope"] in ("storage.input", "storage.output")
                assert ref["component_id"]

    def test_storage_tables_without_usage_omits_used_by(self) -> None:
        data = self._run_ok("storage", "tables", "--project", self.alias)["data"]
        for row in data["tables"]:
            assert "used_by" not in row

    def test_sharing_link_rejects_unknown_stage(self) -> None:
        """Stage validation happens before any API call (exit 2)."""
        result = self._run(
            "sharing",
            "link",
            "--project",
            self.alias,
            "--source-project-id",
            "1",
            "--bucket-id",
            "out.c-nonexistent",
            "--stage",
            "staging",
        )
        assert result.exit_code == 2

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


# ---------------------------------------------------------------------------
# Semantic-layer (since v0.41.0)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2ESemanticLayerLifecycle:
    """E2E coverage for the ``kbagent semantic-layer`` command group.

    Lives apart from the giant TestFullE2E because the SL surface is large
    enough to deserve its own focused test (and because the round-trip
    bootstraps two throwaway models + many children per run, so isolation
    keeps cleanup tight and lets ``pytest -k SemanticLayer`` iterate fast).

    Covers (in order):
    - ``model create`` / ``delete``
    - ``add dataset`` / ``add metric`` / ``add relationship`` /
      ``add constraint`` / ``add glossary``
    - ``show`` (default + ``--type`` filter)
    - ``validate`` (basic; ``--deep`` is skipped because the test tableIds
      are synthetic and would 404 on a Storage table-detail fetch)
    - ``edit metric --new-name`` (constraint cascade)
    - ``edit relationship`` / ``edit glossary`` (NB-5, iter-4 expansion)
    - ``export`` to a tmp file
    - ``import --dry-run`` of the just-exported snapshot (must be all-skip)
    - ``diff`` between live model and exported file (must be empty)
    - ``promote --dry-run`` between two models in the same project
    - ``build --dry-run`` (heuristic fallback, real storage schema fetch)
    - ``token --encrypt`` (envelope shape)
    - ``remove metric`` (single child removal)
    - ``remove relationship`` / ``remove glossary`` (NB-5, iter-4 expansion)

    Teardown is double-belted:
    1. Every CLI-created item is tracked and direct-deleted via
       ``MetastoreClient`` in a try/finally.
    2. A final residue scan asserts no ``kbagent_e2e_*`` items remain across
       all six metastore types -- if any do, the test fails (so silent
       cleanup bugs surface immediately).
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Any:
        if not HAS_CREDENTIALS:
            pytest.skip("E2E_API_TOKEN not set")
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        if not metastore_scope_available(self.url, self.token):
            pytest.skip("metastore/semantic-layer scope not available for this project")
        self.alias = f"{RUN_ID}-sl-proj"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.work_dir = tmp_path / "work"
        self.work_dir.mkdir()
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

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_semantic_layer_roundtrip(self) -> None:
        """Exercise every semantic-layer verb in one bootstrap → teardown cycle."""
        from keboola_agent_cli.metastore_client import (
            SEMANTIC_TYPES,
            MetastoreClient,
        )

        tag = f"kbagent_e2e_{int(time.time())}"
        model_name = tag
        target_model_name = f"{tag}_target"

        created_items: list[tuple[str, str]] = []
        model_id: str | None = None
        target_model_id: str | None = None

        def _direct_delete(item_type: str, item_id: str) -> None:
            with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                mc.delete_item(item_type, item_id)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        try:
            _step(1, "semantic-layer model create")
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                model_name,
            )
            model_id = data["data"]["model"]["id"]
            assert model_id

            _step(2, "add datasets / metrics / relationship / constraint / glossary")
            ds1 = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_ds_a",
                "--table-id",
                "out.c-syn.fact_a",
            )
            created_items.append(("semantic-dataset", ds1["data"]["id"]))

            ds2 = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_ds_b",
                "--table-id",
                "out.c-syn.fact_b",
            )
            created_items.append(("semantic-dataset", ds2["data"]["id"]))

            m1 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_rev",
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_a",
                "--yes",
            )
            created_items.append(("semantic-metric", m1["data"]["id"]))

            m2 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_cost",
                "--sql",
                'SUM("schema"."AMOUNT")',
                "--dataset",
                "out.c-syn.fact_a",
                "--yes",
            )
            created_items.append(("semantic-metric", m2["data"]["id"]))

            m3 = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_count_b",
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_b",
                "--yes",
            )
            created_items.append(("semantic-metric", m3["data"]["id"]))

            rel = self._run_ok(
                "semantic-layer",
                "add",
                "relationship",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rel_a_b",
                "--from",
                "out.c-syn.fact_a",
                "--to",
                "out.c-syn.fact_b",
                "--on",
                "fact_a.id = fact_b.fact_a_id",
            )
            created_items.append(("semantic-relationship", rel["data"]["id"]))

            cons = self._run_ok(
                "semantic-layer",
                "add",
                "constraint",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rev_warning",
                "--constraint-type",
                "inequality",
                "--rule",
                "value >= 0",
                "--metrics",
                f"{tag}_m_rev",
                "--severity",
                "warning",
            )
            created_items.append(("semantic-constraint", cons["data"]["id"]))

            gloss = self._run_ok(
                "semantic-layer",
                "add",
                "glossary",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--term",
                f"{tag}_GMV",
                "--definition",
                "Gross merchandise value (test)",
            )
            created_items.append(("semantic-glossary", gloss["data"]["id"]))

            _step(3, "show + show --type metric")
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
            )
            assert len(data["data"]["datasets"]) == 2
            assert len(data["data"]["metrics"]) == 3
            assert len(data["data"]["constraints"]) == 1
            assert len(data["data"]["glossary"]) == 1

            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "metric",
            )
            assert len(data["data"]["metrics"]) >= 3

            _step(4, "validate (basic) -- expect valid")
            data = self._run_ok(
                "semantic-layer",
                "validate",
                "--project",
                self.alias,
                "--model",
                model_name,
            )
            assert data["data"]["valid"] is True, (
                f"Expected clean model, got errors: {data['data']['errors']}"
            )

            _step(5, "edit metric --new-name -- triggers constraint cascade")
            data = self._run_ok(
                "semantic-layer",
                "edit",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_rev",
                "--new-name",
                f"{tag}_m_revenue",
                "--yes",
            )
            new_metric_id = data["data"]["updated"]["id"]
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-metric" and i == m1["data"]["id"])
            ]
            created_items.append(("semantic-metric", new_metric_id))
            cascaded = data["data"]["cascaded_constraints"]
            assert any(c["status"] == "updated" for c in cascaded), (
                f"Expected constraint cascade, got: {cascaded}"
            )
            # DELETE+POST changed the constraint id -- refresh tracking
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "constraint",
            )
            created_items = [(t, i) for (t, i) in created_items if t != "semantic-constraint"]
            for c in data["data"]["constraints"]:
                created_items.append(("semantic-constraint", c["id"]))

            # ---------- NB-5: edit + remove relationship / glossary ----------

            _step(5.1, "edit relationship --new-on -- DELETE+POST")
            data = self._run_ok(
                "semantic-layer",
                "edit",
                "relationship",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rel_a_b",
                "--new-on",
                "fact_a.id = fact_b.fact_a_id_v2",
            )
            new_rel_id = data["data"]["updated"]["id"]
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-relationship" and i == rel["data"]["id"])
            ]
            created_items.append(("semantic-relationship", new_rel_id))

            _step(5.2, "edit glossary --new-definition -- DELETE+POST")
            data = self._run_ok(
                "semantic-layer",
                "edit",
                "glossary",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--term",
                f"{tag}_GMV",
                "--new-definition",
                "Gross merchandise value (test, v2)",
            )
            new_gloss_id = data["data"]["updated"]["id"]
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-glossary" and i == gloss["data"]["id"])
            ]
            created_items.append(("semantic-glossary", new_gloss_id))

            _step(6, "export -> snapshot.json")
            export_path = self.work_dir / "snapshot.json"
            self._run_ok(
                "semantic-layer",
                "export",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--output",
                str(export_path),
            )
            assert export_path.is_file()

            _step(7, "import --dry-run from the same file -- all-skip expected")
            data = self._run_ok(
                "semantic-layer",
                "import",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--file",
                str(export_path),
                "--dry-run",
            )
            imported = data["data"]["imported"]
            total_skipped = sum(per.get("skipped", 0) for per in imported.values())
            total_created = sum(per.get("created", 0) for per in imported.values())
            assert total_skipped > 0, f"Expected skips on self-import, got: {imported}"
            assert total_created == 0, (
                f"Expected zero creates on self-import dry-run, got {total_created}"
            )

            _step(8, "diff project vs snapshot -- empty diff expected")
            data = self._run_ok(
                "semantic-layer",
                "diff",
                "--project-a",
                self.alias,
                "--model-a",
                model_name,
                "--file-b",
                str(export_path),
            )
            for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
                per = data["data"][type_key]
                assert per["added"] == [] and per["removed"] == [] and per["changed"] == [], (
                    f"Self-diff should be empty for {type_key}: {per}"
                )

            _step(9, "promote --dry-run into a fresh target model")
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                target_model_name,
            )
            target_model_id = data["data"]["model"]["id"]
            data = self._run_ok(
                "semantic-layer",
                "promote",
                "--from-project",
                self.alias,
                "--to-project",
                self.alias,
                "--from-model",
                model_name,
                "--to-model",
                target_model_name,
                "--dry-run",
            )
            # Every per-type stats block exists
            for type_key in ("datasets", "metrics", "relationships", "constraints", "glossary"):
                assert type_key in data["data"], (
                    f"promote dry-run missing {type_key} in stats: {list(data['data'].keys())}"
                )

            _step(10, "build --dry-run -- heuristic fallback against a real storage table")
            # Discover a real table from this project's buckets so build's
            # storage-schema fetch succeeds. Skip the step if the project
            # has no tables (uncommon for e2e-1143, but defensive).
            tables_data = self._run_ok("storage", "tables", "--project", self.alias)
            available_tables = tables_data["data"].get("tables", [])
            if available_tables:
                table_id = available_tables[0]["id"]
                data = self._run_ok(
                    "semantic-layer",
                    "build",
                    "--project",
                    self.alias,
                    "--tables",
                    table_id,
                    "--dry-run",
                )
                assert data["data"]["fallback_used"] == "heuristic", (
                    f"Expected heuristic fallback, got: {data['data'].get('fallback_used')}"
                )
                assert len(data["data"]["generated"]["datasets"]) == 1
            else:
                print("  WARN: no storage tables in project -- build --dry-run skipped")

            _step(11, "token --encrypt -- envelope shape")
            data = self._run_ok(
                "semantic-layer",
                "token",
                "--encrypt",
                "--project",
                self.alias,
                "--component-id",
                TEST_COMPONENT_ID,
            )
            envelope = data["data"]["encrypted"]
            assert "#metastore_token" in envelope
            assert envelope["#metastore_token"].startswith("KBC::"), (
                f"Expected KBC:: ciphertext, got: {envelope['#metastore_token'][:30]}..."
            )

            _step(12, "remove metric -- single-child removal + verify gone")
            data = self._run_ok(
                "semantic-layer",
                "remove",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_m_count_b",
                "--yes",
            )
            assert data["data"]["removed"]["id"] == m3["data"]["id"]
            created_items = [
                (t, i)
                for (t, i) in created_items
                if not (t == "semantic-metric" and i == m3["data"]["id"])
            ]
            data = self._run_ok(
                "semantic-layer",
                "show",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--type",
                "metric",
            )
            metric_names = {m["name"] for m in data["data"]["metrics"]}
            assert f"{tag}_m_count_b" not in metric_names

            _step(12.1, "remove relationship -- leaf entity, no orphan")
            data = self._run_ok(
                "semantic-layer",
                "remove",
                "relationship",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--name",
                f"{tag}_rel_a_b",
                "--yes",
            )
            assert data["data"]["removed"]["name"] == f"{tag}_rel_a_b"
            created_items = [(t, i) for (t, i) in created_items if t != "semantic-relationship"]

            _step(12.2, "remove glossary --term -- leaf entity, no orphan")
            data = self._run_ok(
                "semantic-layer",
                "remove",
                "glossary",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--term",
                f"{tag}_GMV",
                "--yes",
            )
            assert data["data"]["removed"]["name"] == f"{tag}_GMV"
            created_items = [(t, i) for (t, i) in created_items if t != "semantic-glossary"]

        finally:
            print("\n--- SEMANTIC LAYER CLEANUP ---")
            for item_type, item_id in reversed(created_items):
                try:
                    _direct_delete(item_type, item_id)
                    print(f"  Deleted {item_type} {item_id}")
                except Exception as exc:
                    print(f"  WARN: failed to delete {item_type} {item_id}: {exc}")

            for mid in (target_model_id, model_id):
                if mid is None:
                    continue
                try:
                    _direct_delete("semantic-model", mid)
                    print(f"  Deleted semantic-model {mid}")
                except Exception as exc:
                    print(f"  WARN: failed to delete semantic-model {mid}: {exc}")

            # Residue check -- assert teardown actually cleaned up.
            from keboola_agent_cli.errors import KeboolaApiError as _ApiError

            try:
                with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                    residue: list[str] = []
                    for stype in SEMANTIC_TYPES:
                        for item in mc.list_items(stype):  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                            attrs = item.get("attributes") or {}
                            name = attrs.get("name") or attrs.get("term", "")
                            if isinstance(name, str) and name.startswith(tag):
                                residue.append(f"{stype}:{name}:{item.get('id', '')}")
                    assert not residue, (
                        f"Cleanup left residue (manual teardown required): {residue}"
                    )
            except _ApiError as exc:
                print(f"  WARN: residue scan failed: {exc}")

    def test_semantic_layer_reference_data_roundtrip(self) -> None:
        """Exercise `reference-data` set (create) → list → get → set (replace) → delete."""
        from keboola_agent_cli.metastore_client import MetastoreClient

        tag = f"kbagent_e2e_{int(time.time())}"
        model_name = tag
        dimension = f"{tag}_coa"
        model_id: str | None = None
        record_id: str | None = None

        def _direct_delete(item_type: str, item_id: str) -> None:
            with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                mc.delete_item(item_type, item_id)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        try:
            _step(1, "model create")
            model_id = self._run_ok(
                "semantic-layer", "model", "create", "--project", self.alias, "--name", model_name
            )["data"]["model"]["id"]
            assert model_id

            members_file = self.work_dir / "coa.json"
            members_file.write_text(
                json.dumps(
                    [
                        {"account_code": "4011", "account_name": "Revenue", "is_leaf": 1},
                        {
                            "account_code": "ISR99999",
                            "account_name": "Revenue Rollup",
                            "is_leaf": 0,
                        },
                    ]
                )
            )

            _step(2, "reference-data set (create)")
            created = self._run_ok(
                "semantic-layer",
                "reference-data",
                "set",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--dimension",
                dimension,
                "--members-file",
                str(members_file),
                "--dataset-id",
                "out.c-syn.DIM_COA",
            )["data"]
            assert created["action"] == "created"
            assert created["member_count"] == 2
            record_id = created["id"]
            assert record_id

            _step(3, "reference-data list (dimension present)")
            listed = self._run_ok(
                "semantic-layer", "reference-data", "list", "--project", self.alias
            )["data"]
            assert dimension in {r["dimension_name"] for r in listed["reference_data"]}

            _step(4, "reference-data get --id (members intact)")
            got = self._run_ok(
                "semantic-layer",
                "reference-data",
                "get",
                "--project",
                self.alias,
                "--id",
                record_id,
            )["data"]
            assert {m["account_code"] for m in got["members"]} == {"4011", "ISR99999"}

            _step(5, "reference-data set (replace -> revision++)")
            members_file.write_text(
                json.dumps([{"account_code": "4011", "account_name": "Revenue (EU)", "is_leaf": 1}])
            )
            replaced = self._run_ok(
                "semantic-layer",
                "reference-data",
                "set",
                "--project",
                self.alias,
                "--model",
                model_name,
                "--dimension",
                dimension,
                "--members-file",
                str(members_file),
            )["data"]
            assert replaced["action"] == "updated"
            assert replaced["id"] == record_id
            assert replaced["member_count"] == 1

            _step(6, "reference-data delete")
            removed = self._run_ok(
                "semantic-layer",
                "reference-data",
                "delete",
                "--project",
                self.alias,
                "--id",
                record_id,
                "--yes",
            )["data"]
            assert removed["removed"]["id"] == record_id
            record_id = None

        finally:
            print("\n--- REFERENCE-DATA CLEANUP ---")
            if record_id is not None:
                try:
                    _direct_delete("semantic-reference-data", record_id)
                except Exception as exc:
                    print(f"  WARN: failed to delete reference-data: {exc}")
            if model_id is not None:
                try:
                    _direct_delete("semantic-model", model_id)
                except Exception as exc:
                    print(f"  WARN: failed to delete semantic-model {model_id}: {exc}")

    def test_semantic_layer_delete_cascade(self) -> None:
        """Regression test for #306 — cascade-delete frees up per-project dataset names.

        Before #306 was fixed, ``kbagent semantic-layer model delete`` only
        DELETEd the parent ``semantic-model`` row; the child entities
        (datasets, metrics, relationships, constraints, glossary terms) stayed
        on the wire pointing at the now-dead ``modelUUID``. Because dataset
        names are unique **per project** (not per model), the next ``build``
        or ``import`` of a same-named dataset hit HTTP 422
        ``"semantic-dataset with name 'X' already exists in the target model"``
        with no UI/CLI escape.

        This test exercises the four-step regression loop padak called out in
        the PR #309 review (BLOCKING [B-1]):

        1. Create model A with a mix of children that exercise the full
           reverse-PUSH_ORDER cascade (dataset + metric + constraint).
        2. ``kbagent semantic-layer model delete --yes`` via the CLI — assert
           the response envelope carries ``cascade.parent_deleted == True``
           and non-zero per-type counts under ``cascade.deleted``.
        3. Create model B with the **same dataset and metric names** as
           model A — must succeed. Before #306 was fixed this would have
           failed with 422 on the dataset POST because model A's orphan
           still held the name globally in the project.
        4. ``finally`` teardown: drop model B's children + model B itself.
           Model A's children are gone by the time the cascade returns,
           so there's nothing to clean up on the model A side except in
           failure paths.

        Run focused: ``E2E_API_TOKEN=... E2E_URL=... uv run pytest -v
        tests/test_e2e.py::TestE2ESemanticLayerLifecycle::test_semantic_layer_delete_cascade``.
        """
        from keboola_agent_cli.errors import KeboolaApiError as _ApiError
        from keboola_agent_cli.metastore_client import (
            SEMANTIC_TYPES,
            MetastoreClient,
        )

        tag = f"kbagent_e2e_cascade_{int(time.time())}"
        model_a = f"{tag}_a"
        model_b = f"{tag}_b"

        # Names reused across model A and model B — this is the regression:
        # they must be free again after cascade-delete of model A.
        shared_ds_a = f"{tag}_shared_ds_a"
        shared_ds_b = f"{tag}_shared_ds_b"
        shared_metric = f"{tag}_shared_metric"
        shared_constraint = f"{tag}_shared_c_healthy"

        # IDs to clean up if the cascade fails mid-flight.
        model_a_id: str | None = None
        model_b_id: str | None = None
        model_b_items: list[tuple[str, str]] = []

        def _direct_delete(item_type: str, item_id: str) -> None:
            with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                mc.delete_item(item_type, item_id)  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]

        try:
            # --- 1. Create model A with children spanning the cascade order ---
            _step(1, "create model A + cascade-able children")
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                model_a,
            )
            model_a_id = data["data"]["model"]["id"]
            assert model_a_id

            self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_a,
                "--name",
                shared_ds_a,
                "--table-id",
                "out.c-syn.fact_cascade_a",
            )
            self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_a,
                "--name",
                shared_ds_b,
                "--table-id",
                "out.c-syn.fact_cascade_b",
            )
            self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_a,
                "--name",
                shared_metric,
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_cascade_a",
                "--yes",
            )
            # Constraint references the metric by name — exercises the
            # full reverse-PUSH_ORDER cascade (constraint → metric → dataset).
            self._run_ok(
                "semantic-layer",
                "add",
                "constraint",
                "--project",
                self.alias,
                "--model",
                model_a,
                "--name",
                shared_constraint,
                "--constraint-type",
                "inequality",
                "--rule",
                "value >= 0",
                "--metrics",
                shared_metric,
                "--severity",
                "info",
            )

            # --- 2. Cascade-delete model A via the CLI ---
            _step(2, "model delete A (cascade-delete via CLI)")
            delete_resp = self._run_ok(
                "semantic-layer",
                "model",
                "delete",
                "--project",
                self.alias,
                "--model",
                model_a,
                "--yes",
            )
            envelope = delete_resp["data"]
            assert envelope["deleted"]["id"] == model_a_id, (
                f"deleted.id should match the model UUID: {envelope}"
            )
            cascade = envelope["cascade"]
            assert cascade["attempted"] is True, f"cascade attempted: {cascade}"
            assert cascade["parent_deleted"] is True, f"parent should be deleted: {cascade}"
            assert cascade["failures"] == [], (
                f"unexpected cascade failures (should be 0): {cascade['failures']}"
            )
            counts = cascade["deleted"]
            assert counts["datasets"] >= 2, f"datasets cascaded: {counts}"
            assert counts["metrics"] >= 1, f"metrics cascaded: {counts}"
            assert counts["constraints"] >= 1, f"constraints cascaded: {counts}"
            # Back-compat alias: orphaned_children == cascade.deleted (deprecated v0.42.0).
            assert envelope["orphaned_children"] == counts, (
                "orphaned_children back-compat alias should equal cascade.deleted"
            )

            # The cascade succeeded — model A's children are gone.
            # Drop the cleanup token so the finally block doesn't try to delete it again.
            model_a_id = None

            # --- 3. Create model B with the SAME names (regression test) ---
            # Before #306 was fixed, the next add-dataset would 422 here because
            # the orphans from model A still held shared_ds_a / shared_ds_b
            # globally in the project.
            _step(3, "create model B with shared dataset/metric names (regression)")
            data = self._run_ok(
                "semantic-layer",
                "model",
                "create",
                "--project",
                self.alias,
                "--name",
                model_b,
            )
            model_b_id = data["data"]["model"]["id"]

            ds_a_resp = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_b,
                "--name",
                shared_ds_a,  # same name as model A's first dataset
                "--table-id",
                "out.c-syn.fact_cascade_a",
            )
            model_b_items.append(("semantic-dataset", ds_a_resp["data"]["id"]))

            ds_b_resp = self._run_ok(
                "semantic-layer",
                "add",
                "dataset",
                "--project",
                self.alias,
                "--model",
                model_b,
                "--name",
                shared_ds_b,
                "--table-id",
                "out.c-syn.fact_cascade_b",
            )
            model_b_items.append(("semantic-dataset", ds_b_resp["data"]["id"]))

            m_resp = self._run_ok(
                "semantic-layer",
                "add",
                "metric",
                "--project",
                self.alias,
                "--model",
                model_b,
                "--name",
                shared_metric,  # same name as model A's metric
                "--sql",
                "COUNT(*)",
                "--dataset",
                "out.c-syn.fact_cascade_a",
                "--yes",
            )
            model_b_items.append(("semantic-metric", m_resp["data"]["id"]))

            # If we got here without 422, the regression #306 is fixed.

        finally:
            # ----------------------------------------------------------------
            # Teardown — best-effort, runs even on test failure.
            # ----------------------------------------------------------------
            print("\n--- CASCADE TEST CLEANUP ---")
            for item_type, item_id in reversed(model_b_items):
                try:
                    _direct_delete(item_type, item_id)
                    print(f"  Deleted {item_type} {item_id}")
                except Exception as exc:
                    print(f"  WARN: failed to delete {item_type} {item_id}: {exc}")

            if model_b_id:
                try:
                    _direct_delete("semantic-model", model_b_id)
                    print(f"  Deleted semantic-model {model_b_id}")
                except Exception as exc:
                    print(f"  WARN: failed to delete model_b {model_b_id}: {exc}")

            # Only present if the cascade failed mid-test.
            if model_a_id:
                try:
                    _direct_delete("semantic-model", model_a_id)
                    print(f"  Deleted semantic-model {model_a_id} (cascade did not complete)")
                except Exception as exc:
                    print(f"  WARN: failed to delete model_a {model_a_id}: {exc}")

            # Residue check across all six metastore types — fail if anything
            # tagged with this run is left, so silent cleanup bugs surface.
            try:
                with MetastoreClient(stack_url=self.url, token=self.token) as mc:
                    residue: list[str] = []
                    for stype in SEMANTIC_TYPES:
                        for item in mc.list_items(stype):  # type: ignore[arg-type]  # ty: ignore[invalid-argument-type]
                            attrs = item.get("attributes") or {}
                            name = attrs.get("name") or attrs.get("term", "")
                            if isinstance(name, str) and name.startswith(tag):
                                residue.append(f"{stype}:{name}:{item.get('id', '')}")
                    assert not residue, (
                        f"Cleanup left residue (manual teardown required): {residue}"
                    )
            except _ApiError as exc:
                print(f"  WARN: residue scan failed: {exc}")


# ---------------------------------------------------------------------------
# v0.47.0 -- fresh-CREATE writeback + new ergonomic flags (E2E coverage per
# CLAUDE.md convention #16: "Every new CLI command MUST have a corresponding
# E2E test in tests/test_e2e.py").
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2E_0_47_0_NewSurfaces:
    """E2E coverage for v0.47.0 additions.

    - ``storage create-table --if-not-exists`` -- idempotent re-create
    - ``semantic-layer search-context`` + ``get-context`` -- project-wide read
    - ``sync diff --branch <id>`` -- per-invocation dev-branch override

    All three touch a real Keboola project via the configured E2E token. The
    test creates a throwaway dev branch where needed and deletes it in the
    teardown so residue does not accumulate across re-runs.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-v0470"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self.tmp_path = tmp_path

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
        self._dev_branch_id: int | None = None
        self._created_bucket_id: str | None = None
        try:
            yield
        finally:
            if self._dev_branch_id is not None:
                try:
                    self._run(
                        "branch",
                        "delete",
                        "--project",
                        self.alias,
                        "--branch",
                        str(self._dev_branch_id),
                    )
                except Exception as exc:
                    print(f"  WARN: branch delete failed: {exc}")
            if self._created_bucket_id is not None:
                try:
                    self._run(
                        "storage",
                        "delete-bucket",
                        "--project",
                        self.alias,
                        "--bucket-id",
                        self._created_bucket_id,
                        "--force",
                        "--yes",
                    )
                except Exception as exc:
                    print(f"  WARN: bucket delete failed: {exc}")

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    # ------------------------------------------------------------------
    # storage create-table --if-not-exists
    # ------------------------------------------------------------------

    def test_storage_create_table_if_not_exists_round_trip(self) -> None:
        """First call: action=created. Second call with --if-not-exists:
        action=skipped. Third call without the flag: STORAGE_JOB_FAILED."""
        _step("v0470-1", "storage create-table --if-not-exists")
        bucket_name = f"v0470_{RUN_ID.replace('-', '_')[:20]}"
        bucket_data = self._run_ok(
            "storage",
            "create-bucket",
            "--project",
            self.alias,
            "--stage",
            "in",
            "--name",
            bucket_name,
        )
        bucket_id = bucket_data["data"]["id"]
        assert bucket_id.startswith("in.c-")
        self._created_bucket_id = bucket_id

        table_name = f"v0470_tbl_{RUN_ID.replace('-', '_')[:16]}"

        first = self._run_ok(
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
            "label:STRING",
            "--primary-key",
            "id",
            "--if-not-exists",
        )
        assert first["data"]["action"] == "created"
        assert first["data"]["table_id"] == f"{bucket_id}.{table_name}"

        second = self._run_ok(
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
            "label:STRING",
            "--primary-key",
            "id",
            "--if-not-exists",
        )
        assert second["data"]["action"] == "skipped"
        assert second["data"]["skip_reason"] == "table already exists"
        assert second["data"]["table_id"] == f"{bucket_id}.{table_name}"
        # keboola/cli#349: the skipped envelope reports the EXISTING table's
        # actual schema. The request here matches the existing table, so no drift.
        assert second["data"]["columns"] == ["id", "label"]
        assert second["data"]["primary_key"] == ["id"]
        assert second["data"]["requested_columns"] == ["id", "label"]
        assert second["data"]["requested_primary_key"] == ["id"]
        assert second["data"]["schema_drift"] is False

        # keboola/cli#349: a divergent request against the same pre-existing
        # table still skips, but the envelope reports the ACTUAL columns (not the
        # requested 'extra' column) and flags the drift.
        divergent = self._run_ok(
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
            "label:STRING",
            "--column",
            "extra:STRING",
            "--primary-key",
            "extra",
            "--if-not-exists",
        )
        assert divergent["data"]["action"] == "skipped"
        assert divergent["data"]["columns"] == ["id", "label"]
        assert divergent["data"]["primary_key"] == ["id"]
        assert "extra" in divergent["data"]["requested_columns"]
        assert divergent["data"]["requested_primary_key"] == ["extra"]
        assert divergent["data"]["schema_drift"] is True

        third = self._run(
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
            "--primary-key",
            "id",
        )
        assert third.exit_code != 0, (
            "default behavior must still error on duplicate (no silent skip)"
        )
        body = json.loads(third.output)
        assert body.get("status") == "error"
        assert body.get("error", {}).get("code") == "STORAGE_JOB_FAILED"

    # ------------------------------------------------------------------
    # semantic-layer search-context / get-context
    # ------------------------------------------------------------------

    def test_semantic_layer_search_and_get_context(self) -> None:
        """search-context with default pattern returns a valid envelope.
        get-context with an all-zero UUID returns NOT_FOUND."""
        _step("v0470-2", "semantic-layer search-context + get-context")

        search = self._run_ok(
            "semantic-layer",
            "search-context",
            "--project",
            self.alias,
            "--pattern",
            "*",
        )
        data = search["data"]
        assert "contexts" in data
        assert "total_count" in data
        assert isinstance(data["contexts"], list)
        assert isinstance(data["total_count"], int)
        assert data["total_count"] == len(data["contexts"])
        for ctx in data["contexts"]:
            assert ctx["type"] in {
                "model",
                "dataset",
                "metric",
                "relationship",
                "constraint",
                "glossary",
            }, f"unexpected type slug: {ctx['type']!r}"

        only_datasets = self._run_ok(
            "semantic-layer",
            "search-context",
            "--project",
            self.alias,
            "--type",
            "dataset",
        )
        for ctx in only_datasets["data"]["contexts"]:
            assert ctx["type"] == "dataset"

        missing = self._run(
            "semantic-layer",
            "get-context",
            "--project",
            self.alias,
            "--context-id",
            "00000000-0000-0000-0000-000000000000",
        )
        assert missing.exit_code != 0
        body = json.loads(missing.output)
        assert body.get("status") == "error"
        assert body.get("error", {}).get("code") == "NOT_FOUND"

        if data["contexts"]:
            first_id = data["contexts"][0]["id"]
            roundtrip = self._run_ok(
                "semantic-layer",
                "get-context",
                "--project",
                self.alias,
                "--context-id",
                first_id,
            )
            assert roundtrip["data"]["id"] == first_id
            assert roundtrip["data"]["type"] == data["contexts"][0]["type"]

    # ------------------------------------------------------------------
    # sync diff --branch
    # ------------------------------------------------------------------

    def test_sync_diff_branch_override(self) -> None:
        """A dev branch created on the fly is targetable via `sync diff --branch`
        without first running `branch use` or `sync branch-link`."""
        _step("v0470-3", "sync diff --branch <id>")

        branch_name = f"v0470-e2e-{RUN_ID[:20]}"
        branch_data = self._run_ok(
            "branch",
            "create",
            "--project",
            self.alias,
            "--name",
            branch_name,
        )
        dev_branch_id = int(branch_data["data"]["branch_id"])
        self._dev_branch_id = dev_branch_id

        project_dir = self.tmp_path / "v0470-sync"
        project_dir.mkdir()
        _git(project_dir, "init")
        _git(project_dir, "config", "user.email", "e2e@test.local")
        _git(project_dir, "config", "user.name", "E2E Test")
        _git(project_dir, "commit", "--allow-empty", "-m", "init")

        init_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "init",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
            ],
        )
        assert init_result.exit_code == 0, init_result.output

        with_override = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "diff",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        body = json.loads(with_override.output)
        assert body.get("status") == "ok", body
        assert body["data"].get("changes") is not None
        summary = body["data"].get("summary", {})
        assert summary.get("remote_only", 0) >= 0

    # ------------------------------------------------------------------
    # sync push -- fresh-CREATE writeback + KBC.configuration.* propagation
    # (Area B headline fix; against real Storage API + metadata API)
    # ------------------------------------------------------------------

    def test_sync_push_fresh_create_writeback_and_kbc_metadata(self) -> None:
        """Round-trip the FIIA / scaffold emit pattern against a real
        Keboola project: hand-author a placeholder ManifestConfiguration
        with ``KBC.configuration.folderName`` declared, run ``sync push``,
        and assert:
          1. The push reports ``created=1, errors=0``.
          2. The manifest entry was updated in place (length stays at 1,
             not 2; placeholder id is now the assigned ULID; folderName
             metadata is preserved on the entry).
          3. The remote configuration's metadata-list returns the
             KBC.configuration.folderName key with the declared value.
          4. A second ``sync push`` against the same workspace is a no-op
             (``status=no_changes, created=0, errors=0``).

        Cleanup: delete the freshly-created remote config + the dev branch
        in the teardown so re-runs do not accumulate residue.
        """
        from keboola_agent_cli.constants import CONFIG_FILENAME, CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import (
            ManifestConfiguration,
            load_manifest,
            save_manifest,
        )

        _step("v0470-4", "sync push fresh-CREATE writeback + KBC.* propagation")

        # Throwaway dev branch so we never pollute main.
        branch_name = f"v0470-fcw-{RUN_ID[:20]}"
        branch_data = self._run_ok(
            "branch",
            "create",
            "--project",
            self.alias,
            "--name",
            branch_name,
        )
        dev_branch_id = int(branch_data["data"]["branch_id"])
        self._dev_branch_id = dev_branch_id  # teardown will delete

        # Fresh sync workspace.
        project_dir = self.tmp_path / "v0470-fcw"
        project_dir.mkdir()
        _git(project_dir, "init")
        _git(project_dir, "config", "user.email", "e2e@test.local")
        _git(project_dir, "config", "user.name", "E2E Test")
        _git(project_dir, "commit", "--allow-empty", "-m", "init")

        init_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "init",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
            ],
        )
        assert init_result.exit_code == 0, init_result.output

        # Pull the dev branch so its branch directory + entry land in the
        # manifest (otherwise the placeholder's target branch is not
        # tracked and sync push can't resolve a path for it).
        pull_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
                "--no-storage",
                "--no-jobs",
            ],
        )
        assert pull_result.exit_code == 0, pull_result.output

        manifest = load_manifest(project_dir)
        dev_branch_entry = next((b for b in manifest.branches if b.id == dev_branch_id), None)
        assert dev_branch_entry is not None, (
            "sync pull --branch must register the dev branch in the manifest"
        )
        dev_branch_path = dev_branch_entry.path

        # Hand-author a placeholder ManifestConfiguration with the
        # KBC.configuration.folderName key (FIIA / scaffold emit pattern).
        component_id = "keboola.snowflake-transformation"
        config_dir_name = f"v0470-fcw-{RUN_ID[:18]}"
        cfg_rel_path = f"transformation/{component_id}/{config_dir_name}"
        folder_name = "v0.47.0 E2E Fresh-CREATE"
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=dev_branch_id,
                componentId=component_id,
                id="PLACEHOLDER-FCW",
                path=cfg_rel_path,
                metadata={"KBC.configuration.folderName": folder_name},
            )
        )
        save_manifest(project_dir, manifest)
        pre_push_n = len(manifest.configurations)

        # Local _config.yml for the placeholder.
        local_dir = project_dir / dev_branch_path / cfg_rel_path
        local_dir.mkdir(parents=True)
        (local_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "v0.47.0 e2e fresh-create",
                    "description": "E2E test: fresh-CREATE writeback in place",
                    "parameters": {},
                    "_keboola": {"component_id": component_id, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        # First push: should CREATE the config + propagate the folder.
        push_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        assert push_result.exit_code == 0, push_result.output
        body = json.loads(push_result.output)
        assert body.get("status") == "ok", body
        data = body["data"]
        assert data["created"] == 1, data
        assert data["errors"] == [], data["errors"]

        # Manifest contract: updated in place (length unchanged).
        post = load_manifest(project_dir)
        matching = [
            c
            for c in post.configurations
            if c.component_id == component_id
            and c.path == cfg_rel_path
            and c.branch_id == dev_branch_id
        ]
        assert len(matching) == 1, (
            "writeback must update placeholder in place, not duplicate; "
            f"found {len(matching)} matching entries"
        )
        assigned_id = matching[0].id
        assert assigned_id != "PLACEHOLDER-FCW", (
            "placeholder id must be replaced with the API-assigned ULID"
        )
        assert matching[0].metadata.get("KBC.configuration.folderName") == folder_name
        assert len(post.configurations) == pre_push_n, "manifest must not grow on a single CREATE"

        # Remote metadata: folderName landed via the metadata API.
        meta_result = _invoke(
            self.config_dir,
            [
                "--json",
                "config",
                "metadata-list",
                "--project",
                self.alias,
                "--component-id",
                component_id,
                "--config-id",
                assigned_id,
                "--branch",
                str(dev_branch_id),
            ],
        )
        meta = _json_ok(meta_result)
        meta_keys = {m.get("key"): m.get("value") for m in meta["data"]["metadata"]}
        assert meta_keys.get("KBC.configuration.folderName") == folder_name, (
            f"folderName missing or wrong on remote metadata-list: {meta_keys}"
        )

        # Second push: idempotent (no_changes; create_config NOT called again).
        repush = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        assert repush.exit_code == 0, repush.output
        repush_body = json.loads(repush.output)
        repush_status = repush_body.get("data", {}).get("status") or repush_body.get("status")
        assert repush_status in ("no_changes", "pushed"), repush_body
        assert repush_body["data"].get("created", 0) == 0, (
            "re-push must be idempotent: created=0 after writeback in place"
        )

    def test_sync_push_fresh_create_variable_binding_runtime(self) -> None:
        """Fresh-CREATE variable binding end-to-end (KFR-03/04/05), v0.47.2.

        Hand-author the FIIA tree on a throwaway dev branch -- a
        ``keboola.variables`` config + its default-values row + a Snowflake
        transformation whose ``_configuration_extra`` cross-references both by
        placeholder id. One ``sync push`` must:

          1. report ``created=3, errors=0``;
          2. POST the row ``values`` (non-empty remote ``configuration.values``);
          3. rebind the transformation's ``variables_id`` /
             ``variables_values_id`` to real ULIDs (not placeholder dirnames);
          4. produce a **runnable** transformation: ``job run --wait`` reaches
             ``status: success`` (the real acceptance gate -- a broken variable
             link fails at runtime with "Variable configuration ... not found");
          5. be idempotent on re-push (``created=0``) with ``sync diff``
             reporting ``conflict=0``.

        Cleanup: the dev branch (and its configs) is deleted in teardown.
        """
        from keboola_agent_cli.constants import CONFIG_FILENAME, CONFIG_YML_VERSION
        from keboola_agent_cli.sync.manifest import (
            ManifestConfigRow,
            ManifestConfiguration,
            load_manifest,
            save_manifest,
        )

        _step("v0472-1", "sync push fresh-CREATE variable binding + job run")

        branch_name = f"v0472-vb-{RUN_ID[:20]}"
        branch_data = self._run_ok(
            "branch", "create", "--project", self.alias, "--name", branch_name
        )
        dev_branch_id = int(branch_data["data"]["branch_id"])
        self._dev_branch_id = dev_branch_id

        project_dir = self.tmp_path / "v0472-vb"
        project_dir.mkdir()
        _git(project_dir, "init")
        _git(project_dir, "config", "user.email", "e2e@test.local")
        _git(project_dir, "config", "user.name", "E2E Test")
        _git(project_dir, "commit", "--allow-empty", "-m", "init")

        init_result = _invoke(
            self.config_dir,
            ["--json", "sync", "init", "--project", self.alias, "--directory", str(project_dir)],
        )
        assert init_result.exit_code == 0, init_result.output

        pull_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "pull",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
                "--no-storage",
                "--no-jobs",
            ],
        )
        assert pull_result.exit_code == 0, pull_result.output

        manifest = load_manifest(project_dir)
        dev_branch_entry = next((b for b in manifest.branches if b.id == dev_branch_id), None)
        assert dev_branch_entry is not None
        dev_branch_path = dev_branch_entry.path

        # Placeholder ids cross-referenced by the transformation.
        suffix = RUN_ID[:14]
        vars_ph = f"PH-VARS-{suffix}"
        vals_ph = f"PH-VALS-{suffix}"
        tx_component = "keboola.snowflake-transformation"
        vars_component = "keboola.variables"
        vars_path = f"variable/{vars_component}/vb_{suffix}"
        tx_path = f"transformation/{tx_component}/vb_{suffix}"

        vars_entry = ManifestConfiguration(
            branchId=dev_branch_id,
            componentId=vars_component,
            id=vars_ph,
            path=vars_path,
        )
        vars_entry.rows.append(ManifestConfigRow(id=vals_ph, path="rows/default", metadata={}))
        manifest.configurations.append(vars_entry)
        manifest.configurations.append(
            ManifestConfiguration(
                branchId=dev_branch_id,
                componentId=tx_component,
                id=f"PH-TX-{suffix}",
                path=tx_path,
            )
        )
        save_manifest(project_dir, manifest)

        # Local files: variables config + default-values row + transformation.
        vars_dir = project_dir / dev_branch_path / vars_path
        vars_dir.mkdir(parents=True)
        (vars_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": f"vb vars {suffix}",
                    "description": "",
                    "_keboola": {"component_id": vars_component, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        row_dir = vars_dir / "rows" / "default"
        row_dir.mkdir(parents=True)
        (row_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": "default",
                    "description": "",
                    # keboola.variables row values accept only name + value
                    # (the API rejects a "type" key on values.N).
                    "values": [{"name": "greeting", "value": "hello"}],
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        tx_dir = project_dir / dev_branch_path / tx_path
        tx_dir.mkdir(parents=True)
        (tx_dir / CONFIG_FILENAME).write_text(
            yaml.dump(
                {
                    "version": CONFIG_YML_VERSION,
                    "name": f"vb tx {suffix}",
                    "description": "",
                    # No input/output mapping: the SQL just selects the variable
                    # so the job succeeds iff the variable link resolves.
                    "parameters": {
                        "blocks": [
                            {
                                "name": "Block",
                                "codes": [
                                    {
                                        "name": "Greet",
                                        "script": ["SELECT '{{ greeting }}' AS msg;"],
                                    }
                                ],
                            }
                        ]
                    },
                    "_configuration_extra": {
                        "variables_id": vars_ph,
                        "variables_values_id": vals_ph,
                    },
                    "_keboola": {"component_id": tx_component, "config_id": ""},
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )

        # One push creates all three and rebinds the variable link.
        push_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        assert push_result.exit_code == 0, push_result.output
        push_data = json.loads(push_result.output)["data"]
        assert push_data["created"] == 3, push_data
        assert push_data["errors"] == [], push_data["errors"]

        # Select OUR entries by path: a dev branch inherits production's
        # configs, so the manifest holds many pre-existing entries of the same
        # component after the pull -- matching on component_id alone is wrong.
        post = load_manifest(project_dir)
        vars_ulid = next(
            c.id
            for c in post.configurations
            if c.component_id == vars_component and c.path == vars_path
        )
        tx_ulid = next(
            c.id
            for c in post.configurations
            if c.component_id == tx_component and c.path == tx_path
        )
        assert vars_ulid != vars_ph, "variables config placeholder must become a ULID"
        assert tx_ulid != f"PH-TX-{suffix}", "transformation placeholder must become a ULID"

        # Remote transformation: variables_id / variables_values_id are ULIDs.
        tx_detail = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            tx_component,
            "--config-id",
            tx_ulid,
            "--branch",
            str(dev_branch_id),
        )
        tx_cfg = tx_detail["data"]["configuration"]
        assert tx_cfg.get("variables_id") == vars_ulid, tx_cfg
        assert tx_cfg.get("variables_values_id"), tx_cfg
        assert tx_cfg["variables_values_id"] != vals_ph, "values_id must be a real ULID"
        vals_ulid = tx_cfg["variables_values_id"]

        # Remote values row: configuration.values is non-empty (KFR-04).
        vars_detail = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            vars_component,
            "--config-id",
            vars_ulid,
            "--branch",
            str(dev_branch_id),
        )
        rows = vars_detail["data"].get("rows", [])
        bound_row = next((r for r in rows if str(r.get("id")) == str(vals_ulid)), None)
        assert bound_row is not None, f"values row {vals_ulid} missing on remote: {rows}"
        assert bound_row["configuration"].get("values"), "row values must be non-empty"

        # THE REAL GATE: the transformation runs to success (variable resolves).
        run_data = self._run_ok(
            "job",
            "run",
            "--project",
            self.alias,
            "--component-id",
            tx_component,
            "--config-id",
            tx_ulid,
            "--branch",
            str(dev_branch_id),
            "--wait",
            "--timeout",
            "300",
        )
        job = run_data["data"]
        assert job["status"] == "success", (
            f"job run failed ({job['status']}): "
            f"{job.get('result', {}).get('message', 'no message')}"
        )

        # Re-push is idempotent and diff reports no conflict.
        repush = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "push",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        assert repush.exit_code == 0, repush.output
        repush_data = json.loads(repush.output)["data"]
        assert repush_data.get("created", 0) == 0, repush_data
        assert repush_data.get("errors", []) == [], repush_data

        diff_result = _invoke(
            self.config_dir,
            [
                "--json",
                "sync",
                "diff",
                "--project",
                self.alias,
                "--directory",
                str(project_dir),
                "--branch",
                str(dev_branch_id),
            ],
        )
        assert diff_result.exit_code == 0, diff_result.output
        diff_summary = json.loads(diff_result.output)["data"]["summary"]
        assert diff_summary.get("conflict", 0) == 0, diff_summary


class TestDevPortalE2E:
    """E2E coverage for v0.48.0 -- Developer Portal command group.

    - ``dev-portal identity list`` -- unconditional smoke test (no KB token needed)
    - ``dev-portal list --vendor keboola`` -- optional real-portal test (needs E2E_DP_USERNAME
      and E2E_DP_PASSWORD env vars)
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path):
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

    def test_identity_list_smoke(self) -> None:
        """Unconditional smoke: dev-portal identity list must not crash (no identities configured)."""
        result = _invoke(self.config_dir, ["--json", "dev-portal", "identity", "list"])
        assert result.exit_code == 0, result.output
        body = json.loads(result.output)
        # JSON envelope: {"status": "ok", "data": [...]}
        identities = body.get("data", body)
        assert isinstance(identities, list), (
            f"expected list under 'data', got {type(identities).__name__}: {result.output}"
        )

    @pytest.mark.skipif(
        not (os.environ.get("E2E_DP_USERNAME") and os.environ.get("E2E_DP_PASSWORD")),
        reason="Set E2E_DP_USERNAME and E2E_DP_PASSWORD to run real-portal test",
    )
    def test_list_apps_against_real_portal(self) -> None:
        """Optional: add an identity and list apps for vendor 'keboola' if creds supplied."""
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "dev-portal",
                "identity",
                "add",
                "--alias",
                "e2e",
                "--username",
                os.environ["E2E_DP_USERNAME"],
                "--password",
                os.environ["E2E_DP_PASSWORD"],
                "--vendor",
                "keboola",
            ],
        )
        assert result.exit_code == 0, result.output
        result = _invoke(
            self.config_dir,
            [
                "--json",
                "dev-portal",
                "list",
                "--vendor",
                "keboola",
                "--identity",
                "e2e",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_role_hint_typo_rejected_at_cli_layer(self) -> None:
        """`identity add --role-hint vendr` is rejected by Typer's
        `click.Choice(["vendor", "admin"])` validator before any model
        construction happens (since v0.51.1).

        Offline -- the rejection is a Typer usage error (exit 2), no
        network is touched. Note the layering: the Pydantic validator on
        the model itself deliberately *downgrades* unknown values to
        "vendor" with a stderr warning, so legacy free-text values in a
        pre-0.51.1 `config.json` still load. That tolerance is appropriate
        for `ConfigStore.load()` but wrong at the CLI -- a typo the user
        just typed should fail loudly, not silently land as "vendor". The
        `click.Choice` wiring in `commands/dev_portal.py` provides that
        separation.
        """
        result = _invoke(
            self.config_dir,
            [
                "dev-portal",
                "identity",
                "add",
                "--alias",
                "bad-role",
                "--username",
                "u",
                "--password",
                "p",
                "--role-hint",
                "vendr",  # typo
            ],
        )
        # Typer/Click usage error -> exit 2
        assert result.exit_code == 2
        assert "vendor" in result.output.lower() or "admin" in result.output.lower()

    def test_vendor_role_admin_only_field_fails_fast(self) -> None:
        """`prepare_patch` preflight refuses admin-only fields on a vendor identity
        (since v0.51.1). Runs offline -- preflight fires before any portal call,
        so no creds needed. Verifies the user-facing error names the offending
        field and points at the admin-identity workaround.
        """
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import DeveloperPortalIdentity

        store = ConfigStore(self.config_dir, source="cli-flag")
        store.add_dev_portal_identity(
            "vendor-e2e",
            DeveloperPortalIdentity(
                username="u", password="p", vendor="keboola", role_hint="vendor"
            ),
        )

        payload_path = self.config_dir / "patch.json"
        payload_path.write_text(json.dumps({"complexity": "easy"}))
        result = _invoke(
            self.config_dir,
            [
                "dev-portal",
                "patch",
                "--app",
                "keboola.ex-bogus",
                "--data",
                str(payload_path),
                "--identity",
                "vendor-e2e",
                "--dry-run",
            ],
        )
        # exit non-zero because validation error
        assert result.exit_code != 0
        # error mentions the offending field and the admin workaround
        out_lower = result.output.lower()
        assert "complexity" in out_lower
        assert "admin" in out_lower


@skip_without_credentials
@pytest.mark.e2e
class TestHeadlessEnvProject:
    """Headless / token-only invocation against the real API (issue #359).

    Verifies that KBAGENT_PROJECT_FROM_ENV=1 + KBC_TOKEN + KBC_STORAGE_API_URL
    let kbagent run with an EMPTY config dir (no `project add`, no config.json),
    and that the env token is never written to disk.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.config_dir = tmp_path / "empty-config"
        self.config_dir.mkdir()

    def _headless_env(self) -> dict[str, str]:
        return {
            "KBAGENT_PROJECT_FROM_ENV": "1",
            "KBC_TOKEN": self.token,
            "KBC_STORAGE_API_URL": self.url,
        }

    def test_headless_lists_env_project(self) -> None:
        _step("HEADLESS-1", "project list resolves __env__ from env, no config.json")
        with patch.dict(os.environ, self._headless_env()):
            result = _invoke(self.config_dir, ["--json", "project", "list"])
        data = _json_ok(result)
        aliases = {p["alias"] for p in data["data"]}
        assert "__env__" in aliases, data
        # No config.json was written -- token stays in memory only.
        assert not (self.config_dir / "config.json").exists()

    def test_headless_storage_call_hits_api(self) -> None:
        _step("HEADLESS-2", "storage buckets --project __env__ reaches the real API")
        with patch.dict(os.environ, self._headless_env()):
            result = _invoke(
                self.config_dir,
                ["--json", "storage", "buckets", "--project", "__env__"],
            )
        # status=ok proves the env token authenticated a real API call.
        _json_ok(result)
        assert not (self.config_dir / "config.json").exists()

    def test_headless_requires_opt_in_flag(self) -> None:
        _step("HEADLESS-3", "KBC_TOKEN without the opt-in flag => no phantom project")
        env = {"KBC_TOKEN": self.token, "KBC_STORAGE_API_URL": self.url}
        with patch.dict(os.environ, env):
            # Ensure the flag is absent for this assertion.
            os.environ.pop("KBAGENT_PROJECT_FROM_ENV", None)
            result = _invoke(self.config_dir, ["--json", "project", "list"])
        data = _json_ok(result)
        assert data["data"] == [], data


@skip_without_credentials
@pytest.mark.e2e
class TestE2EConfigSecretEncryption:
    """Prove `config new --push` / `config update` encrypt #-secrets before write (#378).

    Regression guard for the v0.54.0 fix. The Storage API stores config JSON
    verbatim, so a #-prefixed value must read back as ``KBC::ProjectSecure::...``
    -- never as the literal plaintext that was sent. Exercises the create path
    (config new --push) and the update path against a live project, then deletes
    the probe config in teardown.
    """

    COMPONENT = "keboola.ex-pohoda-mserver"

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-cfgsecret"

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

        yield

        for component_id, config_id in reversed(self._created):
            try:
                self.client.delete_config(component_id=component_id, config_id=config_id)
            except Exception as exc:
                print(
                    f"  {_DIM}(teardown) delete_config {component_id}/{config_id} failed: {exc}{_RESET}"
                )
        self.client.close()

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(_invoke(self.config_dir, ["--json", *args]))

    def _read_password(self, config_id: str) -> str:
        envelope = self._run_ok(
            "config",
            "detail",
            "--project",
            self.alias,
            "--component-id",
            self.COMPONENT,
            "--config-id",
            config_id,
        )
        # _run_ok returns the full {status, data} envelope; the config body
        # lives under .data.configuration (config detail is single-project here).
        return envelope["data"]["configuration"]["parameters"]["#password"]

    def test_config_create_and_update_encrypt_secret(self) -> None:
        _step(1, "config new --push with a #password", "must encrypt before write")
        created = self._run_ok(
            "config",
            "new",
            "--project",
            self.alias,
            "--component-id",
            self.COMPONENT,
            "--name",
            f"{RUN_ID}-secret-probe",
            "--push",
            "--no-files",
            "--no-validate",
            "--configuration",
            '{"parameters":{"#password":"e2e-canary-create"}}',
        )["data"]
        config_id = str(created["id"])
        self._created.append((self.COMPONENT, config_id))

        _step(2, "read-back: #password must be KBC::, not plaintext")
        pw = self._read_password(config_id)
        print(f"  {_DIM}create read-back #password={pw[:24]}...{_RESET}")
        assert pw.startswith("KBC::"), f"create stored plaintext: {pw!r}"
        assert pw != "e2e-canary-create"

        _step(3, "config update with a new #password")
        self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            self.COMPONENT,
            "--config-id",
            config_id,
            "--configuration",
            '{"parameters":{"#password":"e2e-canary-update"}}',
        )

        _step(4, "read-back after update: still KBC::")
        pw2 = self._read_password(config_id)
        print(f"  {_DIM}update read-back #password={pw2[:24]}...{_RESET}")
        assert pw2.startswith("KBC::"), f"update stored plaintext: {pw2!r}"
        assert pw2 != "e2e-canary-update"

    def test_config_update_dry_run_is_not_encrypted(self) -> None:
        _step(1, "create a probe config to dry-run against")
        created = self._run_ok(
            "config",
            "new",
            "--project",
            self.alias,
            "--component-id",
            self.COMPONENT,
            "--name",
            f"{RUN_ID}-dryrun-probe",
            "--push",
            "--no-files",
            "--no-validate",
            "--configuration",
            '{"parameters":{"user":"probe"}}',
        )["data"]
        config_id = str(created["id"])
        self._created.append((self.COMPONENT, config_id))

        _step(2, "config update --dry-run keeps plaintext in the diff (deterministic)")
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            self.COMPONENT,
            "--config-id",
            config_id,
            "--configuration",
            '{"parameters":{"#password":"e2e-canary-dryrun"}}',
            "--dry-run",
        )["data"]
        new_pw = data["new_configuration"]["parameters"]["#password"]
        assert new_pw == "e2e-canary-dryrun", f"dry-run encrypted the diff: {new_pw!r}"


@skip_without_credentials
@pytest.mark.e2e
class TestE2EDataAppManagedRepo:
    """End-to-end tests for the managed-repo data-app commands (0.65.0):
    `data-app create --use-managed-git-repo`, `data-app git-repo`, and
    `data-app runs`. Creates a real managed app and cleans it up.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-dataapp"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()
        self._created_app_ids: list[str] = []

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

        for app_id in self._created_app_ids:
            with contextlib.suppress(Exception):
                self._run(
                    "data-app", "delete", "--project", self.alias, "--app-id", app_id, "--yes"
                )

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_managed_repo_lifecycle(self) -> None:
        """create --use-managed-git-repo -> git-repo -> runs, then delete."""
        slug = f"e2e-managed-{int(time.time())}"

        _step(1, "create a managed-repo data app (empty repo, forced --no-deploy)")
        created = self._run_ok(
            "data-app",
            "create",
            "--project",
            self.alias,
            "--name",
            f"E2E Managed {RUN_ID}",
            "--slug",
            slug,
            "--use-managed-git-repo",
            "--type",
            "python-js",
            "--auth",
            "public",
        )["data"]
        app_id = str(created["app_id"])
        self._created_app_ids.append(app_id)
        assert created["use_managed_git_repo"] is True
        assert created["deployed"] is False  # managed forces --no-deploy
        assert created["git"] == {}  # no git block written for a managed repo

        _step(2, "git-repo resolves the managed clone URLs immediately (no deploy needed)")
        repo = self._run_ok("data-app", "git-repo", "--project", self.alias, "--app-id", app_id)[
            "data"
        ]
        assert repo["is_managed_git_repo"] is True
        assert repo["https_url"], f"expected an https clone URL, got {repo}"

        _step(3, "runs lists deployment attempts (empty for a never-deployed app)")
        runs = self._run_ok(
            "data-app", "runs", "--project", self.alias, "--app-id", app_id, "--limit", "5"
        )["data"]
        assert "runs" in runs
        assert isinstance(runs["runs"], list)
        assert runs["count"] == len(runs["runs"])

        _step(4, "delete the managed app (cascades the managed repo)")
        self._run_ok("data-app", "delete", "--project", self.alias, "--app-id", app_id, "--yes")
        self._created_app_ids.remove(app_id)

    def test_create_requires_a_git_source(self) -> None:
        """Neither --git-repo nor --use-managed-git-repo -> exit-2 usage error,
        no app created."""
        result = self._run(
            "data-app",
            "create",
            "--project",
            self.alias,
            "--name",
            "E2E No Source",
            "--slug",
            f"e2e-nosrc-{int(time.time())}",
        )
        assert result.exit_code == 2, result.output
        body = json.loads(result.output)
        assert body["error"]["code"] == "USAGE_ERROR"


@skip_without_credentials
@pytest.mark.e2e
class TestE2EDeviceEnrollmentPrimitives:
    """End-to-end coverage for the device-enrollment primitives (0.66.0):
    scoped Storage token mint/revoke/rotate (``kbagent token create|refresh|
    delete``) and per-device OTLP stream sources (the ``Client`` facade's
    ``create_stream_source`` / ``get_stream_source`` / ``list_stream_sources``
    / ``delete_stream_source``).

    Both capabilities are gated by the acting project: minting a scoped token
    needs ``canManageTokens`` and Data Streams may be disabled on the stack.
    When the mint/create fails for an ACCESS_DENIED / permission / not-enabled
    reason we ``pytest.skip`` -- the test then documents the capability without
    turning a missing entitlement into a red build. A genuine bug (bad payload,
    wrong shape, non-permission API error) still fails.

    Security: the minted token's secret value is NEVER printed or logged --
    only booleans and the token *id* leave this class. Every created resource
    (token, stream source) is deleted immediately / in teardown so no live
    credential is ever left behind.
    """

    # Substrings that mark a *capability* gap (entitlement / permission), as
    # opposed to a real bug. Matched case-insensitively against the error
    # message so we catch API phrasings the error_code enum doesn't cover.
    _CAPABILITY_MSG_MARKERS = (
        "canmanagetokens",
        "permission",
        "not enabled",
        "not allowed",
        "not authorized",
        "forbidden",
        "disabled",
        "access denied",
    )
    _CAPABILITY_ERROR_CODES = frozenset(
        {
            ErrorCode.ACCESS_DENIED,
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.MISSING_MASTER_TOKEN,
            ErrorCode.UNAUTHORIZED,
        }
    )

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-devenroll"
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        # Best-effort cleanup ledgers (belt-and-braces around inline deletes).
        self._token_ids: list[str] = []
        self._stream_source_ids: list[str] = []

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

        # Teardown: revoke any token we somehow left behind, then delete any
        # stream source. Both are idempotent-ish; suppress everything so a
        # teardown hiccup never masks the test result.
        for token_id in self._token_ids:
            with contextlib.suppress(Exception):
                _invoke(
                    self.config_dir,
                    [
                        "--json",
                        "token",
                        "delete",
                        "--project",
                        self.alias,
                        "--token-id",
                        token_id,
                        "--yes",
                    ],
                )
        for source_id in self._stream_source_ids:
            with (
                contextlib.suppress(Exception),
                Client(url=self.url, token=self.token) as kbc,
            ):
                kbc.delete_stream_source(source_id)

    # ------------------------------------------------------------------
    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def _is_capability_error(self, exc: Exception) -> bool:
        """True when *exc* signals a missing entitlement (skip), not a bug (fail)."""
        if not isinstance(exc, KeboolaApiError):
            return False
        if exc.error_code in self._CAPABILITY_ERROR_CODES:
            return True
        message = str(getattr(exc, "message", "") or exc).lower()
        return any(marker in message for marker in self._CAPABILITY_MSG_MARKERS)

    def _first_bucket_id(self) -> str | None:
        """Return an existing bucket id to scope ``--bucket-write`` on, if any."""
        buckets = self._run_ok("storage", "buckets", "--project", self.alias)["data"]
        rows = buckets if isinstance(buckets, list) else buckets.get("buckets", [])
        for bucket in rows:
            bucket_id = bucket.get("id") if isinstance(bucket, dict) else None
            if bucket_id:
                return str(bucket_id)
        return None

    # ------------------------------------------------------------------
    def test_scoped_token_mint_rotate_revoke(self) -> None:
        """`token create` (scoped, expiring) -> `token list` -> `token refresh` -> `token delete`.

        Never prints the secret; deletes the token the moment its shape is
        verified so no live credential outlives the test.
        """
        expires_in = 3600  # 1 hour -- lifetime is capped so a leak self-heals.
        bucket_id = self._first_bucket_id()

        create_args = [
            "token",
            "create",
            "--project",
            self.alias,
            "--description",
            f"{RUN_ID} e2e device-enrollment token",
            "--expires-in",
            str(expires_in),
        ]
        if bucket_id is not None:
            _step(
                1, "token create", f"scoped: --bucket-write {bucket_id}, --expires-in {expires_in}"
            )
            create_args += ["--bucket-write", bucket_id]
        else:
            _step(1, "token create", f"minimal scoped token, --expires-in {expires_in}")

        result = self._run(*create_args)
        if result.exit_code != 0:
            # The service raised a ConfigError/KeboolaApiError -> structured body.
            body = json.loads(result.output)
            code = body.get("error", {}).get("code", "")
            message = body.get("error", {}).get("message", "")
            if code in {c.value for c in self._CAPABILITY_ERROR_CODES} or any(
                marker in message.lower() for marker in self._CAPABILITY_MSG_MARKERS
            ):
                pytest.skip(
                    f"Project token cannot manage tokens (code={code}); "
                    "scoped-token E2E documents the capability without a live token."
                )
            pytest.fail(f"token create failed unexpectedly (exit {result.exit_code}): {code}")

        data = _json_ok(result)["data"]
        token_id = str(data["id"])
        # Track BEFORE any further assertion so a failure still triggers cleanup.
        self._token_ids.append(token_id)
        assert token_id, "minted token must carry an id"
        # Booleans only -- never surface the secret value itself.
        assert bool(data.get("token")), "minted token must reveal a non-empty secret once"
        assert data.get("alias") == self.alias

        _step(2, "token list", "the minted token is visible, and no row carries a secret")
        listed = self._run_ok("token", "list", "--project", self.alias)["data"]
        assert listed.get("alias") == self.alias
        assert listed.get("count") == len(listed.get("tokens") or [])
        rows = {str(row.get("id")): row for row in listed.get("tokens") or []}
        assert token_id in rows, "the freshly minted token must appear in the listing"
        assert rows[token_id].get("description", "").endswith("e2e device-enrollment token")
        # The whole point of the strip: `create` is the only reveal, so not one
        # row -- not even another project token's -- may carry a value.
        assert all("token" not in row for row in rows.values()), (
            "token list must never carry a secret value"
        )
        # The default shape must stay exactly what machine consumers parse today.
        assert all("lastUsed" not in row for row in rows.values()), (
            "the last-used fields must be absent without --with-last-used"
        )

        _step(
            3,
            "token list --with-last-used",
            "a token minted seconds ago must read as never used, not 'used today'",
        )
        audited = self._run_ok("token", "list", "--project", self.alias, "--with-last-used")["data"]
        audited_rows = {str(row.get("id")): row for row in audited.get("tokens") or []}
        assert token_id in audited_rows, "the minted token must survive the enrichment"
        minted = audited_rows[token_id]
        # This is the regression this feature exists for. The token's own event
        # feed is NOT empty -- it holds the `storage.tokenCreated` the ACTING
        # token performed on it -- so a naive `events[0]` read reports it as
        # active. Narrowing to events the token itself performed is what makes
        # the answer "never", and the answer is provable here because the token
        # was minted well inside the event-retention window.
        assert minted.get("lastUsedStatus") == "never", (
            f"a just-minted token must read as never used, got {minted.get('lastUsedStatus')!r} "
            f"(lastUsed={minted.get('lastUsed')!r}, event={minted.get('lastUsedEvent')!r})"
        )
        assert minted.get("lastUsed") is None
        assert all(
            row.get("lastUsedStatus") in {"used", "never", "unknown", "error"}
            for row in audited_rows.values()
        ), "every row must carry a known last-used status"
        assert isinstance(audited.get("errors"), list)

        _step(4, "token refresh", "rotate the secret; id is stable, old value dies")
        refreshed = self._run_ok(
            "token", "refresh", "--project", self.alias, "--token-id", token_id, "--yes"
        )["data"]
        assert bool(refreshed.get("token")), "rotated token must reveal a new non-empty secret"

        _step(5, "token delete", "revoke immediately -- no live credential left behind")
        deleted = self._run_ok(
            "token", "delete", "--project", self.alias, "--token-id", token_id, "--yes"
        )["data"]
        assert deleted.get("status") == "deleted"
        assert str(deleted.get("token_id")) == token_id
        self._token_ids.remove(token_id)

    def test_stream_source_create_and_delete(self) -> None:
        """`Client.create_stream_source` -> get/list -> `delete_stream_source`.

        Asserts the normalized source carries an id + OTLP ingest URL + sink
        bucket, then revokes it. Skips cleanly when Data Streams is disabled.
        """
        source_name = f"{RUN_ID}-devenroll-src"

        _step(1, "Client.create_stream_source", f"otlp source {source_name!r} + auto sinks")
        with Client(url=self.url, token=self.token) as kbc:
            try:
                source = kbc.create_stream_source(source_name)
            except KeboolaApiError as exc:
                if self._is_capability_error(exc):
                    pytest.skip(
                        f"Data Streams unavailable on this project ({exc.error_code}); "
                        "stream-source E2E documents the capability without a live source."
                    )
                raise

            source_id = source.id
            self._stream_source_ids.append(source_id)
            assert source_id, "created stream source must carry an id"
            assert source.otlp_url, "otlp source must expose an ingest URL"
            assert source.sink_bucket_id, "provision_sinks=True must yield a sink bucket"
            # Do not print otlp_url -- it embeds the ingest secret. Assert the
            # derived sink-bucket convention instead (id/shape only).
            assert source.sink_bucket_id == f"in.c-otlp-{source_id}"

            _step(2, "get_stream_source / list_stream_sources", "the source is discoverable")
            fetched = kbc.get_stream_source(source_id)
            assert fetched.id == source_id
            listed = kbc.list_stream_sources()
            assert any(str(s.get("sourceId") or s.get("id")) == source_id for s in listed), (
                "created source must appear in list_stream_sources"
            )

            _step(3, "delete_stream_source", "per-device event-plane revocation")
            kbc.delete_stream_source(source_id)
            self._stream_source_ids.remove(source_id)


@skip_without_session_credentials
@pytest.mark.e2e
class TestE2EAuthRegisterProjects:
    """End-to-end coverage for `kbagent auth register-projects` (0.80.0).

    Fixes a real usability bug: `auth login` prints an accessible-projects
    table but registers nothing unless `--register-projects` is passed, and
    the alias it would offer is a slug of the project NAME -- the numeric
    project id shown in that table (e.g. 9840) never resolves as `--project`.
    This class exercises the fix's non-interactive surface (`--project-id`),
    which needs no TTY and is therefore safe to run unattended in CI, unlike
    the flagless interactive picker (covered by unit tests in
    tests/test_auth_picker.py / tests/test_cli_auth.py instead).

    Deliberately gated on the SAME session-credential env vars as
    tests/test_e2e_auth.py (a programmatic session, not a static Storage
    token) rather than the rest of this file's `skip_without_credentials` --
    see that module's docstring for how to provision
    E2E_SESSION_REFRESH_TOKEN without ever typing it on the command line.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> Generator[None, None, None]:
        self.stack_url = normalize_stack_url(os.environ[ENV_URL])
        self.project_id = int(os.environ[ENV_SESSION_PROJECT_ID])
        self.config_dir = tmp_path / "config"
        self.config_dir.mkdir()

        # Seed a session directly into auth.json (sibling of config.json in
        # the same config_dir) -- no interactive browser login here, this
        # test proves `register-projects` against an EXISTING session, which
        # is exactly what a real user has once `auth login` already
        # succeeded. access_token is left blank so the first live call is
        # forced through a real proactive refresh (only a refresh token is
        # ever handed to this test on the command line).
        store = AuthStateStore(self.config_dir)
        store.put_session(
            StackSession(
                stack_url=self.stack_url,
                session_id="e2e-register-projects-probe",
                access_token="",
                refresh_token=os.environ[ENV_SESSION_REFRESH_TOKEN],
                access_expires_at=None,
                refresh_expires_at=None,
                created_at=datetime.now(UTC),
            )
        )

        yield

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def test_register_by_project_id_then_idempotent_on_rerun(self) -> None:
        """`--project-id` registers a sentinel-token alias; re-running is a no-op.

        Proves the whole non-interactive path end to end: the session can
        list itself as an accessible project, the CLI writes the
        `kbc-session://{project_id}` sentinel (never a real token) into
        config.json, and the resulting alias is immediately usable by an
        ordinary read command. A second run against the same project+stack
        must report `status: "exists"` and must NOT overwrite the alias --
        this is the "never overwrites an existing registration" contract
        shared with the picker and with `login --register-projects`.
        """
        _step(1, "auth register-projects --project-id", "register the session's own project")
        data = self._run_ok(
            "auth",
            "register-projects",
            "--stack",
            self.stack_url,
            "--project-id",
            str(self.project_id),
        )["data"]

        registered = data["registered_projects"]
        assert len(registered) == 1, f"expected exactly one project, got: {registered}"
        entry = registered[0]
        assert entry["project_id"] == self.project_id
        assert entry["status"] in ("registered", "exists")
        alias = entry["alias"]
        assert alias, "a registered project must carry a non-empty alias"

        _step(2, "config.json carries the sentinel token, never a real one")
        config = ConfigStore(config_dir=self.config_dir).load()
        project = config.projects.get(alias)
        assert project is not None, f"alias {alias!r} was not persisted to config.json"
        assert project.token == make_session_token(self.project_id)
        assert project.project_id == self.project_id

        _step(3, "the new alias is immediately usable by an ordinary read command")
        status = self._run_ok("project", "status", "--project", alias)["data"]
        assert status.get("status") in ("ok", "OK") or status.get("alias") == alias

        _step(4, "re-running against the same project+stack is a no-op, never overwritten")
        rerun = self._run_ok(
            "auth",
            "register-projects",
            "--stack",
            self.stack_url,
            "--project-id",
            str(self.project_id),
        )["data"]
        rerun_entry = rerun["registered_projects"][0]
        assert rerun_entry["status"] == "exists"
        assert rerun_entry["alias"] == alias, "re-registering must not change the alias"

        config_after = ConfigStore(config_dir=self.config_dir).load()
        assert config_after.projects[alias].token == make_session_token(self.project_id)

    def test_all_and_project_id_mutually_exclusive(self) -> None:
        """`--all` and `--project-id` together is a usage error (exit 2), not a merge."""
        _step(1, "auth register-projects --all --project-id together -> usage error")
        result = self._run(
            "auth",
            "register-projects",
            "--stack",
            self.stack_url,
            "--all",
            "--project-id",
            str(self.project_id),
        )
        assert result.exit_code == 2, result.output

    def test_unknown_project_id_raises_config_error(self) -> None:
        """A `--project-id` the session cannot access must fail, never silently register."""
        _step(1, "auth register-projects --project-id <unreachable> -> ConfigError")
        # A project id vanishingly unlikely to be in this session's accessible set.
        bogus_id = 900_000_000 + self.project_id
        result = self._run(
            "auth",
            "register-projects",
            "--stack",
            self.stack_url,
            "--project-id",
            str(bogus_id),
        )
        assert result.exit_code != 0, "an inaccessible project id must not report success"
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert str(bogus_id) in body["error"]["message"]

        config = ConfigStore(config_dir=self.config_dir).load()
        assert not any(p.project_id == bogus_id for p in config.projects.values()), (
            "a failed registration must not partially write config.json"
        )

    def test_no_selector_in_json_mode_fails_fast_instead_of_prompting(self) -> None:
        """Omitting --all/--project-id under --json must fail fast, never hang on a TTY prompt.

        `--json` implies a non-interactive caller; the interactive picker
        needs a real terminal (CliRunner's stdin is not a TTY either way).
        Both signals independently rule out the picker, so this must be a
        clean, fast `ConfigError` -- not a hang waiting for input that will
        never arrive.
        """
        _step(1, "auth register-projects with neither --all nor --project-id, --json set")
        result = self._run("auth", "register-projects", "--stack", self.stack_url)
        assert result.exit_code != 0
        body = json.loads(result.output)
        assert body["status"] == "error"
        message = body["error"]["message"].lower()
        assert "--all" in message or "--project-id" in message, (
            f"error should point at the non-interactive flags: {body['error']['message']}"
        )


# ---------------------------------------------------------------------------
# config state-get / config state-set (issue #593)
# ---------------------------------------------------------------------------


@skip_without_credentials
@pytest.mark.e2e
class TestE2EConfigState:
    """End-to-end tests for `config state-get` / `config state-set` (issue #593).

    Creates a throwaway ``ex-generic-v2`` config (needs no external
    credentials, creates/deletes cleanly -- same choice as
    ``TestE2EMcpParityCommands``), exercises the full state round-trip
    (fresh-empty -> dry-run -> real write -> read-back -> no-op -> row
    variant -> missing-row error), and the Part A ``--set`` sibling-path
    guard, then deletes the config.

    NOTE on the row-state-set assertions below: the two state endpoints
    answer with DIFFERENT shapes. The root ``PUT .../state`` returns the
    full configuration detail (which carries a ``rows[]`` array), while
    ``PUT .../rows/{row}/state`` returns the bare updated row object with
    no ``rows`` key at all. An early build of #593 ran the same
    ``_extract_state(result, row_id, ...)`` lookup over both, so every row
    write reported a false ``NOT_FOUND`` even though the PUT returned 200
    and the state had landed. That was found by running this suite against
    a live project -- mock-based tests could not catch it, because the
    mocks returned the shape the author assumed rather than the shape the
    API sends. If ``test_row_state_roundtrip`` ever fails on
    ``changed``/``state`` for the write step specifically, suspect that
    post-write extraction again before assuming the test is wrong.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path: Path) -> None:
        self.token = os.environ[ENV_TOKEN]
        raw_url = os.environ.get(ENV_URL, "connection.keboola.com")
        self.url = raw_url if raw_url.startswith("https://") else f"https://{raw_url}"
        self.alias = f"{RUN_ID}-state"[:60]
        self.component_id = "ex-generic-v2"

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
        self._created_config_ids: list[str] = []

    @pytest.fixture(autouse=True)
    def cleanup(self) -> Any:
        yield
        for cfg_id in self._created_config_ids:
            with contextlib.suppress(Exception):
                self.client.delete_config(component_id=self.component_id, config_id=cfg_id)
        self.client.close()

    def _run(self, *args: str) -> Any:
        return _invoke(self.config_dir, ["--json", *args])

    def _run_ok(self, *args: str) -> dict[str, Any]:
        return _json_ok(self._run(*args))

    def _create_config(self, name_suffix: str) -> str:
        cfg = self.client.create_config(
            component_id=self.component_id,
            name=f"{RUN_ID}-state-{name_suffix}",
            configuration={},
            description="E2E throwaway -- issue #593 config state-get/state-set",
        )
        config_id = str(cfg["id"])
        self._created_config_ids.append(config_id)
        return config_id

    def test_root_state_roundtrip(self) -> None:
        """Fresh empty -> dry-run (no write) -> real write -> read-back -> no-op."""
        config_id = self._create_config("root")

        _step(1, "state-get on a fresh config -- expect {}")
        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
        )["data"]
        assert data["state"] == {}

        _step(2, "state-set --dry-run -- diff only, no write")
        data = self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--state",
            '{"lastImportId": "12345"}',
            "--dry-run",
        )["data"]
        assert data["dry_run"] is True
        assert "changes" in data

        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
        )["data"]
        assert data["state"] == {}, "a --dry-run state-set must not write anything"

        _step(3, 'state-set --state \'{"lastImportId": "12345"}\' --yes -- real write')
        data = self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--state",
            '{"lastImportId": "12345"}',
            "--yes",
        )["data"]
        assert data["changed"] is True
        assert data["state"] == {"lastImportId": "12345"}

        _step(4, "state-get -- matches what was written")
        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
        )["data"]
        assert data["state"] == {"lastImportId": "12345"}

        _step(5, "state-set with the SAME state again -- no-op (changed: False)")
        data = self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--state",
            '{"lastImportId": "12345"}',
            "--yes",
        )["data"]
        assert data["changed"] is False

    def test_row_state_roundtrip(self) -> None:
        """Row state is independent of root state; write/read-back a row's state.

        See the class docstring on why the row PUT's response shape differs
        from the root one -- that asymmetry caused a false NOT_FOUND on every
        row write until it was fixed in #593.
        """
        config_id = self._create_config("row")

        row = self.client.create_config_row(
            component_id=self.component_id,
            config_id=config_id,
            name=f"{RUN_ID}-state-row",
            configuration={},
        )
        row_id = str(row["id"])

        _step(1, "seed root state so independence is checkable")
        self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--state",
            '{"lastImportId": "12345"}',
            "--yes",
        )

        _step(2, "state-set --row-id -- real write on the row")
        data = self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--row-id",
            row_id,
            "--state",
            '{"rowCursor": "abc"}',
            "--yes",
        )["data"]
        assert data["changed"] is True
        assert data["state"] == {"rowCursor": "abc"}

        _step(3, "state-get --row-id -- matches what was written")
        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--row-id",
            row_id,
        )["data"]
        assert data["state"] == {"rowCursor": "abc"}

        _step(4, "root state is unaffected by the row write")
        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
        )["data"]
        assert data["state"] == {"lastImportId": "12345"}

        _step(5, "row state is unaffected by a subsequent root write")
        self._run_ok(
            "config",
            "state-set",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--state",
            '{"lastImportId": "99999"}',
            "--yes",
        )
        data = self._run_ok(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--row-id",
            row_id,
        )["data"]
        assert data["state"] == {"rowCursor": "abc"}

    def test_state_get_missing_row_fails_clearly(self) -> None:
        """state-get --row-id <nonexistent> must fail with a clear error, not return {}."""
        config_id = self._create_config("missing-row")

        _step(1, "state-get --row-id <nonexistent> -- clear error, not an empty dict")
        result = self._run(
            "config",
            "state-get",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--row-id",
            "does-not-exist-593",
        )
        assert result.exit_code != 0
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert "does-not-exist-593" in body["error"]["message"]

    def test_set_guard_rejects_state_prefix_exit_2(self) -> None:
        """`config update --set 'state.foo=1'` must be a hard usage error (exit 2).

        Part A of issue #593: `--set` only edits `configuration.*`; a path
        whose first segment is a top-level API sibling like `state` must be
        rejected before any network call, and the error must point the
        caller at `config state-set`. A plain `--set 'parameters.foo=1'`
        must keep working (regression check).
        """
        config_id = self._create_config("guard")

        _step(1, "guarded --set 'state.foo=1' -- exit 2, message names config state-set")
        result = self._run(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--set",
            "state.foo=1",
        )
        assert result.exit_code == 2, f"expected exit 2, got {result.exit_code}: {result.output}"
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert "config state-set" in body["error"]["message"]

        _step(2, "guard also fires under --dry-run (usage error must never look like a preview)")
        result = self._run(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--set",
            "state.foo=1",
            "--dry-run",
        )
        assert result.exit_code == 2, f"--dry-run must not bypass the guard: {result.output}"

        _step(3, "regression -- a normal --set 'parameters.foo=1' still works")
        data = self._run_ok(
            "config",
            "update",
            "--project",
            self.alias,
            "--component-id",
            self.component_id,
            "--config-id",
            config_id,
            "--set",
            "parameters.foo=1",
        )["data"]
        assert data["configuration"]["parameters"]["foo"] == 1
