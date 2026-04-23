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

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"

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
