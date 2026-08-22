"""Tests for storage describe CLI commands via CliRunner.

Covers describe-bucket, describe-table, describe-column, and describe-batch.
Follows the CLI test pattern from test_workspace_cli.py with patched services.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.storage_service import StorageService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


def _setup_config(config_dir: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


class TestStorageDescribeBucket:
    """Tests for `kbagent storage describe-bucket`."""

    def test_describe_bucket_json(self, tmp_path: Path) -> None:
        """describe-bucket --text returns structured JSON on success."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.describe_bucket.return_value = {
            "project_alias": "prod",
            "bucket_id": "in.c-my-bucket",
            "description": "My bucket description",
            "result": [{"id": "1", "key": "KBC.description", "value": "My bucket description"}],
            "message": "Description set on bucket 'in.c-my-bucket' in project 'prod'.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-bucket",
                    "--project",
                    "prod",
                    "--bucket-id",
                    "in.c-my-bucket",
                    "--text",
                    "My bucket description",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["bucket_id"] == "in.c-my-bucket"
        assert output["data"]["description"] == "My bucket description"
        mock_storage.describe_bucket.assert_called_once_with(
            alias="prod",
            bucket_id="in.c-my-bucket",
            description="My bucket description",
            branch_id=None,
        )

    def test_describe_bucket_missing_source(self, tmp_path: Path) -> None:
        """describe-bucket without --text/--file/--stdin exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-bucket",
                    "--project",
                    "prod",
                    "--bucket-id",
                    "in.c-my-bucket",
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        mock_storage.describe_bucket.assert_not_called()

    def test_describe_bucket_api_error(self, tmp_path: Path) -> None:
        """describe-bucket propagates API errors with appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.describe_bucket.side_effect = KeboolaApiError(
            message="Bucket not found",
            status_code=404,
            error_code="BUCKET_NOT_FOUND",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-bucket",
                    "--project",
                    "prod",
                    "--bucket-id",
                    "in.c-missing",
                    "--text",
                    "desc",
                ],
            )

        assert result.exit_code != 0
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "BUCKET_NOT_FOUND"


class TestStorageDescribeTable:
    """Tests for `kbagent storage describe-table`."""

    def test_describe_table_json(self, tmp_path: Path) -> None:
        """describe-table --text returns structured JSON on success."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.describe_table.return_value = {
            "project_alias": "prod",
            "table_id": "in.c-bucket.orders",
            "description": "All orders",
            "result": [{"id": "2", "key": "KBC.description", "value": "All orders"}],
            "message": "Description set on table 'in.c-bucket.orders' in project 'prod'.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-table",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--text",
                    "All orders",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["table_id"] == "in.c-bucket.orders"
        assert output["data"]["description"] == "All orders"
        mock_storage.describe_table.assert_called_once_with(
            alias="prod",
            table_id="in.c-bucket.orders",
            description="All orders",
            branch_id=None,
        )

    def test_describe_table_missing_source(self, tmp_path: Path) -> None:
        """describe-table without description source exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-table",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        mock_storage.describe_table.assert_not_called()

    def test_describe_table_from_file(self, tmp_path: Path) -> None:
        """describe-table --file reads description from a text file."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        desc_file = tmp_path / "desc.txt"
        desc_file.write_text("Description from file", encoding="utf-8")

        mock_storage = MagicMock()
        mock_storage.describe_table.return_value = {
            "project_alias": "prod",
            "table_id": "in.c-bucket.orders",
            "description": "Description from file",
            "result": [],
            "message": "Description set.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-table",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--file",
                    str(desc_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_storage.describe_table.assert_called_once_with(
            alias="prod",
            table_id="in.c-bucket.orders",
            description="Description from file",
            branch_id=None,
        )


class TestStorageDescribeColumn:
    """Tests for `kbagent storage describe-column`."""

    def test_describe_column_json(self, tmp_path: Path) -> None:
        """describe-column with NAME=DESC pairs returns structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.describe_columns.return_value = {
            "project_alias": "prod",
            "table_id": "in.c-bucket.orders",
            "columns": {"order_id": "Unique order identifier", "total": "Order total in USD"},
            "result": [],
            "message": "Descriptions set for 2 column(s) on table 'in.c-bucket.orders' in project 'prod'.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-column",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--column",
                    "order_id=Unique order identifier",
                    "--column",
                    "total=Order total in USD",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["table_id"] == "in.c-bucket.orders"
        assert output["data"]["columns"]["order_id"] == "Unique order identifier"
        assert output["data"]["columns"]["total"] == "Order total in USD"
        mock_storage.describe_columns.assert_called_once_with(
            alias="prod",
            table_id="in.c-bucket.orders",
            columns={"order_id": "Unique order identifier", "total": "Order total in USD"},
            branch_id=None,
        )

    def test_describe_column_missing_equals(self, tmp_path: Path) -> None:
        """describe-column with malformed --column (no =) exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-column",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--column",
                    "order_id_no_equals",
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        mock_storage.describe_columns.assert_not_called()

    def test_describe_column_empty_name(self, tmp_path: Path) -> None:
        """describe-column with empty column name exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-column",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--column",
                    "=description with empty name",
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        mock_storage.describe_columns.assert_not_called()

    def test_describe_column_with_branch(self, tmp_path: Path) -> None:
        """describe-column passes branch_id when --branch is given."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.describe_columns.return_value = {
            "project_alias": "prod",
            "table_id": "in.c-bucket.orders",
            "columns": {"col1": "Column 1"},
            "result": [],
            "message": "Descriptions set for 1 column(s).",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-column",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-bucket.orders",
                    "--column",
                    "col1=Column 1",
                    "--branch",
                    "999",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_storage.describe_columns.assert_called_once_with(
            alias="prod",
            table_id="in.c-bucket.orders",
            columns={"col1": "Column 1"},
            branch_id=999,
        )


class TestStorageDescribeBatch:
    """Tests for `kbagent storage describe-batch`."""

    def test_describe_batch_json(self, tmp_path: Path) -> None:
        """describe-batch with a valid YAML file returns structured JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        batch_file = tmp_path / "descriptions.yaml"
        batch_file.write_text(
            "buckets:\n"
            "  in.c-sales: Sales bucket\n"
            "tables:\n"
            "  in.c-sales.orders: All orders\n"
            "columns:\n"
            "  in.c-sales.orders:\n"
            "    order_id: Unique order ID\n",
            encoding="utf-8",
        )

        mock_storage = MagicMock()
        mock_storage.describe_batch.return_value = {
            "project_alias": "prod",
            "applied": [
                {"type": "bucket", "id": "in.c-sales", "description": "Sales bucket"},
                {"type": "table", "id": "in.c-sales.orders", "description": "All orders"},
                {
                    "type": "columns",
                    "id": "in.c-sales.orders",
                    "columns": {"order_id": "Unique order ID"},
                },
            ],
            "errors": [],
            "message": "Batch complete: 3 applied, 0 errors.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-batch",
                    "--project",
                    "prod",
                    "--from-file",
                    str(batch_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["applied"]) == 3
        assert output["data"]["errors"] == []
        # JSON mode must never wire a progress callback; human mode does.
        mock_storage.describe_batch.assert_called_once_with(
            alias="prod",
            from_file=batch_file,
            branch_id=None,
            progress_callback=None,
        )

    def test_describe_batch_file_not_found(self, tmp_path: Path) -> None:
        """describe-batch raises ValueError when YAML file does not exist."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        missing_file = tmp_path / "missing.yaml"

        mock_storage = MagicMock()
        mock_storage.describe_batch.side_effect = ValueError(
            f"Batch file not found: {missing_file}"
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-batch",
                    "--project",
                    "prod",
                    "--from-file",
                    str(missing_file),
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"

    def test_describe_batch_malformed_shape_json_envelope(self, tmp_path: Path) -> None:
        """Issue #640: a list under `tables:` answers --json, not a traceback.

        Runs the REAL StorageService so the shape check actually executes; the
        client factory raises if reached, proving the rejection happens before
        any API call.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        batch_file = tmp_path / "bad.yaml"
        batch_file.write_text(
            "tables:\n  - table_id: in.c-test.orders\n    columns:\n      id: Surrogate key\n",
            encoding="utf-8",
        )

        def _no_client(url: str, token: str) -> MagicMock:
            raise AssertionError("describe-batch must not reach the API on a malformed file")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = StorageService(
                config_store=store,
                client_factory=_no_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-batch",
                    "--project",
                    "prod",
                    "--from-file",
                    str(batch_file),
                ],
            )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        assert "'tables' must be a mapping of table ID to description" in output["error"]["message"]
        assert "got a list" in output["error"]["message"]

    def test_describe_batch_human_mode_wires_progress_callback(self, tmp_path: Path) -> None:
        """Human mode must pass a progress_callback; JSON mode must not."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        batch_file = tmp_path / "descriptions.yaml"
        batch_file.write_text("buckets:\n  in.c-sales: Sales data\n", encoding="utf-8")

        mock_storage = MagicMock()
        mock_storage.describe_batch.return_value = {
            "project_alias": "prod",
            "applied": [{"type": "bucket", "id": "in.c-sales", "description": "Sales data"}],
            "errors": [],
            "applied_count": 1,
            "error_count": 0,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            # No --json flag -> human mode -> progress_callback must be wired.
            result = runner.invoke(
                app,
                [
                    "storage",
                    "describe-batch",
                    "--project",
                    "prod",
                    "--from-file",
                    str(batch_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        kwargs = mock_storage.describe_batch.call_args.kwargs
        assert kwargs["alias"] == "prod"
        assert kwargs["from_file"] == batch_file
        assert kwargs["branch_id"] is None
        # Key assertion: human mode supplies a callable; JSON mode supplies None.
        assert callable(kwargs["progress_callback"])

    def test_describe_batch_partial_errors(self, tmp_path: Path) -> None:
        """describe-batch with partial errors still exits 0 (errors collected, not raised)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        batch_file = tmp_path / "partial.yaml"
        batch_file.write_text("buckets:\n  in.c-good: Good\n  in.c-bad: Bad\n", encoding="utf-8")

        mock_storage = MagicMock()
        mock_storage.describe_batch.return_value = {
            "project_alias": "prod",
            "applied": [{"type": "bucket", "id": "in.c-good", "description": "Good"}],
            "errors": [{"type": "bucket", "id": "in.c-bad", "error": "Not found"}],
            "message": "Batch complete: 1 applied, 1 error.",
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "describe-batch",
                    "--project",
                    "prod",
                    "--from-file",
                    str(batch_file),
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["applied"]) == 1
        assert len(output["data"]["errors"]) == 1


class TestStorageBucketDetailHumanRender:
    """Tests for `kbagent storage bucket-detail` human-mode rendering.

    Pins the dialect-aware render branches added in 0.25.3:
    - Snowflake -> "Snowflake DB" / "Snowflake schema" labels and the
      legacy double-quoted path in the table column.
    - BigQuery  -> "BigQuery project" / "BigQuery dataset" labels and the
      backtick-quoted path in the table column.

    JSON-mode shape is covered by ``test_storage_describe_service.py
    ::TestGetBucketDetailBackendPaths``; this suite exercises the
    ``commands/storage.py`` render branch that runs only when ``--json`` is
    not passed.
    """

    def _run(self, tmp_path: Path, mock_storage_payload: dict) -> tuple[int, str, MagicMock]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.get_bucket_detail.return_value = mock_storage_payload

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage

            result = runner.invoke(
                app,
                [
                    "storage",
                    "bucket-detail",
                    "--project",
                    "prod",
                    "--bucket-id",
                    mock_storage_payload["bucket_id"],
                ],
            )

        return result.exit_code, result.output, mock_storage

    def test_snowflake_human_render_uses_legacy_labels_and_double_quotes(
        self, tmp_path: Path
    ) -> None:
        """Snowflake render branch surfaces the BC `Snowflake DB`/`Snowflake schema`
        labels and the double-quoted path in the table column."""
        exit_code, output, _ = self._run(
            tmp_path,
            {
                "project_alias": "prod",
                "project_id": 901,
                "bucket_id": "in.c-sales",
                "display_name": "sales",
                "stage": "in",
                "description": "",
                "backend": "snowflake",
                "is_linked": False,
                "metadata": [],
                "source_project_id": None,
                "source_project_name": "",
                "source_bucket_id": "",
                "sql_dialect": "snowflake",
                "snowflake_database": "SAPI_901",
                "snowflake_schema": "in.c-sales",
                "tables": [
                    {
                        "id": "in.c-sales.orders",
                        "name": "orders",
                        "display_name": "orders",
                        "is_alias": False,
                        "snowflake_path": '"SAPI_901"."in.c-sales"."orders"',
                        "sql_path": '"SAPI_901"."in.c-sales"."orders"',
                    }
                ],
                "table_count": 1,
            },
        )

        assert exit_code == 0, output
        assert "Snowflake DB: SAPI_901" in output
        assert "Snowflake schema: in.c-sales" in output
        # Path table contains the double-quoted Snowflake path. Rich may wrap
        # long lines, so anchor on the unique table.column suffix.
        assert '"in.c-sales"."orders"' in output
        # BigQuery-only labels must NOT show up on a Snowflake bucket.
        assert "BigQuery project" not in output
        assert "BigQuery dataset" not in output

    def test_bigquery_human_render_uses_backticks_and_dataset_labels(self, tmp_path: Path) -> None:
        """BigQuery render branch surfaces `BigQuery dataset` and the backtick
        path; with empty `bigquery_project` the helper hint is shown."""
        exit_code, output, _ = self._run(
            tmp_path,
            {
                "project_alias": "prod",
                "project_id": 9621,
                "bucket_id": "in.c-test-bucket",
                "display_name": "test-bucket",
                "stage": "in",
                "description": "",
                "backend": "bigquery",
                "is_linked": False,
                "metadata": [],
                "source_project_id": None,
                "source_project_name": "",
                "source_bucket_id": "",
                "sql_dialect": "bigquery",
                "bigquery_project": "",
                "bigquery_dataset": "in_c_test_bucket",
                "tables": [
                    {
                        "id": "in.c-test-bucket.test-bigquery-table",
                        "name": "test-bigquery-table",
                        "display_name": "test-bigquery-table",
                        "is_alias": False,
                        "bigquery_path": "`in_c_test_bucket`.`test-bigquery-table`",
                        "sql_path": "`in_c_test_bucket`.`test-bigquery-table`",
                    }
                ],
                "table_count": 1,
            },
        )

        assert exit_code == 0, output
        # Empty bigquery_project triggers the helper line. Rich wraps long
        # lines so the marker text may be split across lines -- anchor on a
        # short unique fragment.
        assert "BigQuery project" in output
        assert "not exposed by Storage API" in output
        assert "BigQuery dataset: in_c_test_bucket" in output
        # Snowflake-only labels must NOT leak onto a BigQuery render.
        assert "Snowflake DB" not in output
        assert "Snowflake schema" not in output
        # Backtick-quoted path appears in the table column. Rich's box-drawing
        # may insert wrapping spaces; anchor on the unique trailing segment.
        assert "`test-bigquery-table`" in output

    def test_bigquery_human_render_with_project_omits_helper_hint(self, tmp_path: Path) -> None:
        """When `bigquery_project` is populated (BYODB), the render shows it
        directly and skips the 'not exposed by Storage API' helper line."""
        exit_code, output, _ = self._run(
            tmp_path,
            {
                "project_alias": "prod",
                "project_id": 9621,
                "bucket_id": "in.c-foo",
                "display_name": "foo",
                "stage": "in",
                "description": "",
                "backend": "bigquery",
                "is_linked": False,
                "metadata": [],
                "source_project_id": None,
                "source_project_name": "",
                "source_bucket_id": "",
                "sql_dialect": "bigquery",
                "bigquery_project": "kbc-bq-9621",
                "bigquery_dataset": "dataset_foo",
                "tables": [
                    {
                        "id": "in.c-foo.bar",
                        "name": "bar",
                        "display_name": "bar",
                        "is_alias": False,
                        "bigquery_path": "`kbc-bq-9621`.`dataset_foo`.`bar`",
                        "sql_path": "`kbc-bq-9621`.`dataset_foo`.`bar`",
                    }
                ],
                "table_count": 1,
            },
        )

        assert exit_code == 0, output
        assert "BigQuery project: kbc-bq-9621" in output
        assert "BigQuery dataset: dataset_foo" in output
        # Helper hint is for the empty-project case only -- must not appear
        # when project IS populated.
        assert "not exposed by Storage API" not in output


_MIGRATE_RESULT = {
    "project_alias": "prod",
    "dry_run": True,
    "tables_scanned": 2,
    "tables_migrated": 0,
    "migrated": [{"table_id": "in.c-bucket.orders", "columns": {"order_id": "Unique id"}}],
    "skipped": [],
    "pruned_orphans": [],
    "errors": [],
    "message": "Would migrate 1 table(s) of 2 scanned in project 'prod'.",
}


class TestStorageDescribeMigrate:
    """Tests for `kbagent storage describe-migrate`."""

    def _invoke(
        self,
        tmp_path: Path,
        args: list[str],
        mock_storage: MagicMock,
        stdin: str | None = None,
    ):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage
            return runner.invoke(app, args, input=stdin)

    def test_describe_migrate_dry_run_json(self, tmp_path: Path) -> None:
        """--dry-run --json emits the scan report and performs no write."""
        mock_storage = MagicMock()
        mock_storage.describe_migrate.return_value = _MIGRATE_RESULT

        result = self._invoke(
            tmp_path,
            [
                "--json",
                "storage",
                "describe-migrate",
                "--project",
                "prod",
                "--dry-run",
            ],
            mock_storage,
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert output["data"]["migrated"][0]["table_id"] == "in.c-bucket.orders"
        mock_storage.describe_migrate.assert_called_once_with(
            alias="prod",
            table_ids=None,
            bucket_id=None,
            prune_orphans=False,
            dry_run=True,
            branch_id=None,
        )

    def test_describe_migrate_scope_conflict_exits_2(self, tmp_path: Path) -> None:
        """--table-id together with --bucket-id is a usage error."""
        mock_storage = MagicMock()

        result = self._invoke(
            tmp_path,
            [
                "--json",
                "storage",
                "describe-migrate",
                "--project",
                "prod",
                "--table-id",
                "in.c-bucket.orders",
                "--bucket-id",
                "in.c-bucket",
            ],
            mock_storage,
        )

        assert result.exit_code == 2, f"Expected 2, got {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"
        mock_storage.describe_migrate.assert_not_called()

    def test_describe_migrate_confirm_abort_makes_no_writes(self, tmp_path: Path) -> None:
        """Answering 'n' at the confirm prompt leaves the scan as the only call."""
        mock_storage = MagicMock()
        mock_storage.describe_migrate.return_value = _MIGRATE_RESULT

        result = self._invoke(
            tmp_path,
            ["storage", "describe-migrate", "--project", "prod"],
            mock_storage,
            stdin="n\n",
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Aborted" in result.output
        assert mock_storage.describe_migrate.call_count == 1
        assert mock_storage.describe_migrate.call_args.kwargs["dry_run"] is True

    def test_describe_migrate_yes_skips_confirm(self, tmp_path: Path) -> None:
        """--yes applies straight away (single, non-dry-run call)."""
        mock_storage = MagicMock()
        applied = dict(_MIGRATE_RESULT, dry_run=False, tables_migrated=1)
        mock_storage.describe_migrate.return_value = applied

        result = self._invoke(
            tmp_path,
            [
                "storage",
                "describe-migrate",
                "--project",
                "prod",
                "--bucket-id",
                "in.c-bucket",
                "--prune-orphans",
                "--yes",
            ],
            mock_storage,
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_storage.describe_migrate.assert_called_once_with(
            alias="prod",
            table_ids=None,
            bucket_id="in.c-bucket",
            prune_orphans=True,
            dry_run=False,
            branch_id=None,
        )
        assert "in.c-bucket.orders" in result.output

    def test_describe_migrate_table_ids_repeatable(self, tmp_path: Path) -> None:
        mock_storage = MagicMock()
        mock_storage.describe_migrate.return_value = dict(_MIGRATE_RESULT, dry_run=False)

        result = self._invoke(
            tmp_path,
            [
                "--json",
                "storage",
                "describe-migrate",
                "--project",
                "prod",
                "--table-id",
                "in.c-bucket.a",
                "--table-id",
                "in.c-bucket.b",
                "--yes",
            ],
            mock_storage,
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_storage.describe_migrate.assert_called_once_with(
            alias="prod",
            table_ids=["in.c-bucket.a", "in.c-bucket.b"],
            bucket_id=None,
            prune_orphans=False,
            dry_run=False,
            branch_id=None,
        )

    def test_describe_migrate_api_error_maps_exit_code(self, tmp_path: Path) -> None:
        mock_storage = MagicMock()
        mock_storage.describe_migrate.side_effect = KeboolaApiError(
            message="Bucket not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        result = self._invoke(
            tmp_path,
            [
                "--json",
                "storage",
                "describe-migrate",
                "--project",
                "prod",
                "--dry-run",
            ],
            mock_storage,
        )

        assert result.exit_code == 1, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"

    def test_describe_migrate_is_a_write_operation(self) -> None:
        """The permission firewall must classify the command as a write."""
        from keboola_agent_cli.permissions import OPERATION_REGISTRY

        assert OPERATION_REGISTRY["storage.describe-migrate"] == "write"


class TestStorageTableDetailLegacyWarning:
    """table-detail surfaces legacy column-description keys (#624)."""

    def _invoke(self, tmp_path: Path, args: list[str], payload: dict):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        store = _setup_config(config_dir, {"prod": {"token": TEST_TOKEN}})

        mock_storage = MagicMock()
        mock_storage.get_table_detail.return_value = payload

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.StorageService") as MockStorageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)
            MockStorageService.return_value = mock_storage
            return runner.invoke(app, args)

    @staticmethod
    def _payload(legacy: list[str]) -> dict:
        return {
            "project_alias": "prod",
            "table_id": "in.c-bucket.orders",
            "name": "orders",
            "display_name": "orders",
            "bucket_id": "in.c-bucket",
            "backend": "snowflake",
            "description": "",
            "columns": ["order_id"],
            "column_details": [{"name": "order_id", "type": "STRING"}],
            "primary_key": [],
            "rows_count": 0,
            "data_size_bytes": 0,
            "is_alias": False,
            "last_import_date": "",
            "last_change_date": "",
            "created": "",
            "metadata": [],
            "legacy_column_descriptions": legacy,
        }

    def test_table_detail_human_warns_on_legacy(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            ["storage", "table-detail", "--project", "prod", "--table-id", "in.c-bucket.orders"],
            self._payload(["order_id"]),
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "legacy" in result.output.lower()
        assert "describe-migrate" in result.output

    def test_table_detail_human_silent_without_legacy(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            ["storage", "table-detail", "--project", "prod", "--table-id", "in.c-bucket.orders"],
            self._payload([]),
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "describe-migrate" not in result.output

    def test_table_detail_json_exposes_legacy_key(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path,
            [
                "--json",
                "storage",
                "table-detail",
                "--project",
                "prod",
                "--table-id",
                "in.c-bucket.orders",
            ],
            self._payload(["order_id"]),
        )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["data"]["legacy_column_descriptions"] == ["order_id"]
