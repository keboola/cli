"""Tests for storage swap-tables: client, service, and CLI."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import TABLE_DATA_JOB_MAX_WAIT
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


def _make_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    config = AppConfig(
        projects={
            "test": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            )
        },
    )
    store.save(config)
    return store


def _make_service(store: ConfigStore, mock_client: MagicMock) -> StorageService:
    return StorageService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


# ---------------------------------------------------------------------------
# Client layer
# ---------------------------------------------------------------------------


class TestSwapTablesClient:
    """Tests for KeboolaClient.swap_tables() - HTTP layer."""

    def test_correct_url_and_body(self, httpx_mock) -> None:
        """POSTs to /v2/storage/branch/{branch}/tables/{tid}/swap with targetTableId in body.

        Storage API responds with a queued storage job (operationName=tableSwap);
        the client polls to completion. Returning ``status: success`` from the
        first response avoids exercising the poll loop in this unit test.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/9999/tables/in.c-foo.data/swap",
            method="POST",
            json={
                "id": 386488069,
                "status": "success",
                "operationName": "tableSwap",
                "operationParams": {
                    "branchId": 9999,
                    "targetTableStringId": "in.c-foo.data_change_log",
                },
            },
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.swap_tables(
            table_id="in.c-foo.data",
            target_table_id="in.c-foo.data_change_log",
            branch_id=9999,
        )

        # Returned dict is the completed storage job
        assert result["status"] == "success"
        assert result["operationName"] == "tableSwap"

        # Verify request body contained targetTableId
        sent_request = httpx_mock.get_request()
        body = json.loads(sent_request.content.decode("utf-8"))
        assert body == {"targetTableId": "in.c-foo.data_change_log"}
        client.close()

    def test_dotted_table_id_passed_verbatim_in_path(self, httpx_mock) -> None:
        """Dotted/dashed table IDs land in the path as-is.

        Dots and dashes are RFC 3986 unreserved, so ``quote(..., safe="")``
        does not percent-encode them; this verifies the table ID is placed
        in the path verbatim (a reserved char, if present, would be encoded).
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/1/tables/in.c-bucket-with-dashes.tbl/swap",
            method="POST",
            json={"id": 1, "status": "success", "operationName": "tableSwap"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        client.swap_tables(
            table_id="in.c-bucket-with-dashes.tbl",
            target_table_id="in.c-bucket-with-dashes.tbl2",
            branch_id=1,
        )
        client.close()

    def test_polls_async_job_to_completion(self, httpx_mock) -> None:
        """If POST returns ``status: waiting``, client polls /v2/storage/jobs/{id}."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.a/swap",
            method="POST",
            json={"id": 555, "status": "waiting", "operationName": "tableSwap"},
            status_code=200,
        )
        # Subsequent poll returns success
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/555",
            method="GET",
            json={"id": 555, "status": "success", "operationName": "tableSwap"},
            status_code=200,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        result = client.swap_tables(
            table_id="in.c-foo.a",
            target_table_id="in.c-foo.b",
            branch_id=42,
        )
        assert result["status"] == "success"
        client.close()

    def test_api_error_propagates(self, httpx_mock) -> None:
        """Storage API 4xx propagates as KeboolaApiError."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/9999/tables/in.c-foo.x/swap",
            method="POST",
            json={"error": "Source and target tables have different column sets"},
            status_code=400,
        )

        client = KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )
        with pytest.raises(KeboolaApiError):
            client.swap_tables(
                table_id="in.c-foo.x",
                target_table_id="in.c-foo.y",
                branch_id=9999,
            )
        client.close()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestSwapTablesService:
    """Tests for StorageService.swap_tables()."""

    def test_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.swap_tables.return_value = {"status": "ok"}
        service = _make_service(store, mock_client)

        result = service.swap_tables(
            alias="test",
            table_id="in.c-foo.data",
            target_table_id="in.c-foo.data_change_log",
            branch_id=9999,
        )

        assert result["project_alias"] == "test"
        assert result["branch_id"] == 9999
        assert result["table_id"] == "in.c-foo.data"
        assert result["target_table_id"] == "in.c-foo.data_change_log"
        assert result["dry_run"] is False
        assert result["response"] == {"status": "ok"}
        mock_client.swap_tables.assert_called_once_with(
            table_id="in.c-foo.data",
            target_table_id="in.c-foo.data_change_log",
            branch_id=9999,
            max_wait=TABLE_DATA_JOB_MAX_WAIT,
        )
        mock_client.close.assert_called_once()

    def test_dry_run_skips_client_call(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.swap_tables(
            alias="test",
            table_id="in.c-foo.a",
            target_table_id="in.c-foo.b",
            branch_id=42,
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert "response" not in result
        mock_client.swap_tables.assert_not_called()

    def test_no_branch_raises_config_error(self, tmp_path: Path) -> None:
        """Mandatory branch enforcement: swap-tables without --branch or active branch raises ConfigError before any HTTP."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError, match="requires a branch"):
            service.swap_tables(
                alias="test",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=None,
            )
        mock_client.swap_tables.assert_not_called()

    def test_same_table_id_raises_config_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError, match="two different tables"):
            service.swap_tables(
                alias="test",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.a",
                branch_id=42,
            )
        mock_client.swap_tables.assert_not_called()

    def test_unknown_project(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(ConfigError):
            service.swap_tables(
                alias="nonexistent",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
            )

    def test_api_error_propagates(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.swap_tables.side_effect = KeboolaApiError(
            "Schema mismatch", status_code=400, error_code="STORAGE_VALIDATION"
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.swap_tables(
                alias="test",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
            )
        # Service must close the client even when the API call raises
        # (try/finally contract -- regression guard for the lifecycle).
        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


class TestSwapTablesCLI:
    """CLI tests for `kbagent storage swap-tables`."""

    def _project_with_active_branch(self, store: ConfigStore, branch_id: int) -> None:
        config = store.load()
        config.projects["test"].active_branch_id = branch_id
        store.save(config)

    def test_swap_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 9999)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.swap_tables.return_value = {
                "project_alias": "test",
                "branch_id": 9999,
                "table_id": "in.c-foo.data",
                "target_table_id": "in.c-foo.data_change_log",
                "dry_run": False,
                "response": {"status": "ok"},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "swap-tables",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                    "--target-table-id",
                    "in.c-foo.data_change_log",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["table_id"] == "in.c-foo.data"
        assert data["target_table_id"] == "in.c-foo.data_change_log"
        assert data["branch_id"] == 9999

        svc.swap_tables.assert_called_once_with(
            alias="test",
            table_id="in.c-foo.data",
            target_table_id="in.c-foo.data_change_log",
            branch_id=9999,
            dry_run=False,
            timeout=TABLE_DATA_JOB_MAX_WAIT,
        )

    def test_swap_dry_run(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 42)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.swap_tables.return_value = {
                "project_alias": "test",
                "branch_id": 42,
                "table_id": "in.c-foo.a",
                "target_table_id": "in.c-foo.b",
                "dry_run": True,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "swap-tables",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--target-table-id",
                    "in.c-foo.b",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["dry_run"] is True
        svc.swap_tables.assert_called_once()
        call_kwargs = svc.swap_tables.call_args.kwargs
        assert call_kwargs["dry_run"] is True

    def test_swap_explicit_branch_overrides_active(self, tmp_path: Path) -> None:
        """--branch flag takes precedence over project's active_branch_id."""
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 100)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.swap_tables.return_value = {
                "project_alias": "test",
                "branch_id": 555,
                "table_id": "in.c-foo.a",
                "target_table_id": "in.c-foo.b",
                "dry_run": False,
                "response": {"status": "ok"},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "swap-tables",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--target-table-id",
                    "in.c-foo.b",
                    "--branch",
                    "555",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.swap_tables.call_args.kwargs
        assert call_kwargs["branch_id"] == 555

    def test_swap_missing_branch_fails_clearly(self, tmp_path: Path) -> None:
        """Without an active branch and without --branch, ConfigError -> exit 5."""
        store = _make_store(tmp_path)
        # No active_branch_id set on project

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.swap_tables.side_effect = ConfigError("swap-tables requires a branch.")
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "swap-tables",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--target-table-id",
                    "in.c-foo.b",
                    "--yes",
                ],
            )

        assert result.exit_code == 5
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert "requires a branch" in payload["error"]["message"]


# ---------------------------------------------------------------------------
# Job wait budget (issue #713)
# ---------------------------------------------------------------------------


class TestSwapTablesTimeout:
    """The swap job budget is caller-controllable and defaults to 5 minutes.

    A swap moves a populated table into place; it used to inherit the 60s
    ``STORAGE_JOB_MAX_WAIT`` meant for metadata jobs, so a large BigQuery
    table reported ``STORAGE_JOB_TIMEOUT`` for a job that was still running
    and would succeed. Giving up locally never cancels the job.
    """

    def test_service_default_is_table_data_budget(self, tmp_path: Path) -> None:
        """No --timeout means TABLE_DATA_JOB_MAX_WAIT, not the 60s metadata default."""
        from keboola_agent_cli.constants import STORAGE_JOB_MAX_WAIT

        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.swap_tables.return_value = {"status": "ok"}
        service = _make_service(store, mock_client)

        service.swap_tables(
            alias="test",
            table_id="in.c-foo.a",
            target_table_id="in.c-foo.b",
            branch_id=42,
        )

        max_wait = mock_client.swap_tables.call_args.kwargs["max_wait"]
        assert max_wait == TABLE_DATA_JOB_MAX_WAIT
        assert max_wait > STORAGE_JOB_MAX_WAIT

    def test_service_forwards_explicit_timeout(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.swap_tables.return_value = {"status": "ok"}
        service = _make_service(store, mock_client)

        service.swap_tables(
            alias="test",
            table_id="in.c-foo.a",
            target_table_id="in.c-foo.b",
            branch_id=42,
            timeout=900.0,
        )

        assert mock_client.swap_tables.call_args.kwargs["max_wait"] == 900.0

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_service_rejects_non_positive_timeout(self, tmp_path: Path, bad: float) -> None:
        """0.0 must be rejected, not silently promoted to the default.

        ``timeout or DEFAULT`` would treat a falsy-but-real 0 as "unset";
        the guard branches on ``is None`` instead.
        """
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError) as exc_info:
            service.swap_tables(
                alias="test",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
                timeout=bad,
            )

        assert exc_info.value.error_code == ErrorCode.INVALID_ARGUMENT
        mock_client.swap_tables.assert_not_called()

    def test_service_rejects_non_positive_timeout_on_dry_run(self, tmp_path: Path) -> None:
        """The guard runs before the dry-run short-circuit, so --dry-run validates too."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.swap_tables(
                alias="test",
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
                dry_run=True,
                timeout=0.0,
            )

    def test_client_forwards_budget_to_the_poller(self, httpx_mock) -> None:
        """swap_tables passes max_wait through to _wait_for_storage_job.

        Asserted on the kwarg, not on elapsed time: an httpx mock never sees
        the budget, so dropping the argument would leave every wire test green
        while the swap silently fell back to the 60s metadata default.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.a/swap",
            method="POST",
            json={"id": 777, "status": "waiting", "operationName": "tableSwap"},
            status_code=200,
        )

        client = KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
        with patch.object(
            KeboolaClient,
            "_wait_for_storage_job",
            return_value={"id": 777, "status": "success"},
        ) as poller:
            client.swap_tables(
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
                max_wait=900.0,
            )
        client.close()

        assert poller.call_args.kwargs["max_wait"] == 900.0

    def test_expired_budget_reports_the_job_as_still_running(self, httpx_mock) -> None:
        """An exhausted budget raises STORAGE_JOB_TIMEOUT naming the live job.

        The message must not read as "nothing happened": kbagent stops
        watching, the swap keeps running server-side. That is also why the
        code is retryable (exit 4), not a plain failure.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.a/swap",
            method="POST",
            json={"id": 777, "status": "waiting", "operationName": "tableSwap"},
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/777",
            method="GET",
            json={"id": 777, "status": "waiting", "operationName": "tableSwap"},
            status_code=200,
        )

        client = KeboolaClient(stack_url="https://connection.keboola.com", token=TEST_TOKEN)
        # The deadline is only checked at the top of each iteration, so the
        # budget cannot expire before the first poll however small it is.
        # Skipping the real sleep keeps the test instant.
        with (
            patch("keboola_agent_cli.client._core.time.sleep"),
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.swap_tables(
                table_id="in.c-foo.a",
                target_table_id="in.c-foo.b",
                branch_id=42,
                max_wait=0.0001,
            )
        client.close()

        exc = exc_info.value
        assert exc.error_code == ErrorCode.STORAGE_JOB_TIMEOUT
        assert exc.retryable is True
        assert "777" in exc.message
        assert "continues running" in exc.message

    def test_cli_forwards_timeout(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.swap_tables.return_value = {
                "project_alias": "test",
                "branch_id": 42,
                "table_id": "in.c-foo.a",
                "target_table_id": "in.c-foo.b",
                "dry_run": False,
                "response": {"status": "success"},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "swap-tables",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.a",
                    "--target-table-id",
                    "in.c-foo.b",
                    "--branch",
                    "42",
                    "--timeout",
                    "900",
                    "--yes",
                ],
            )

        assert result.exit_code == 0
        assert svc.swap_tables.call_args.kwargs["timeout"] == 900.0
