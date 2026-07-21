"""Tests for storage table snapshots (issue #512): client, service, and CLI.

Covers the five commands -- snapshots, snapshot-create, snapshot-detail,
snapshot-delete, table-from-snapshot -- across the three layers. Endpoint
shapes mirror the live API (verified on connection.us-east4.gcp.keboola.com,
2026-07-22): snapshot create and table-from-snapshot return queued storage
jobs; list/detail/delete are synchronous.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.snapshot_service import SnapshotService

runner = CliRunner()

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

SNAPSHOT = {
    "id": "954",
    "description": "before migration",
    "createdTime": "2026-07-22T00:05:24+0200",
    "type": "table",
    "creatorToken": {"id": 1, "description": "petr@example.com"},
}


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


def _make_service(store: ConfigStore, mock_client: MagicMock) -> SnapshotService:
    return SnapshotService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )


# ---------------------------------------------------------------------------
# Client layer
# ---------------------------------------------------------------------------


class TestSnapshotClient:
    """HTTP-shape tests for the five KeboolaClient snapshot methods."""

    def _client(self) -> KeboolaClient:
        return KeboolaClient(
            stack_url="https://connection.keboola.com",
            token=TEST_TOKEN,
        )

    def test_create_snapshot_posts_and_polls_job(self, httpx_mock) -> None:
        """POST tables/{id}/snapshots with description; 202 job polled to results."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-foo.data/snapshots",
            method="POST",
            json={"id": 555, "status": "waiting", "operationName": "tableSnapshotCreate"},
            status_code=202,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/555",
            method="GET",
            json={"id": 555, "status": "success", "results": {"id": "954"}},
            status_code=200,
        )

        client = self._client()
        results = client.create_table_snapshot(
            table_id="in.c-foo.data", description="before migration"
        )
        client.close()

        assert results == {"id": "954"}
        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content) == {"description": "before migration"}

    def test_create_snapshot_branch_prefix(self, httpx_mock) -> None:
        """branch_id=42 routes through /v2/storage/branch/42/tables/.../snapshots."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/tables/in.c-foo.data/snapshots",
            method="POST",
            json={"id": 1, "status": "success", "results": {"id": "7"}},
            status_code=202,
        )

        client = self._client()
        results = client.create_table_snapshot(table_id="in.c-foo.data", branch_id=42)
        client.close()

        assert results == {"id": "7"}
        # No description passed -> empty JSON body (the API treats it as optional).
        assert json.loads(httpx_mock.get_requests()[0].content) == {}

    def test_list_snapshots_plain_and_with_limit(self, httpx_mock) -> None:
        """GET tables/{id}/snapshots returns the raw list; limit becomes a query param."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-foo.data/snapshots",
            method="GET",
            json=[SNAPSHOT],
            status_code=200,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/tables/in.c-foo.data/snapshots?limit=5",
            method="GET",
            json=[SNAPSHOT],
            status_code=200,
        )

        client = self._client()
        assert client.list_table_snapshots(table_id="in.c-foo.data") == [SNAPSHOT]
        assert client.list_table_snapshots(table_id="in.c-foo.data", limit=5) == [SNAPSHOT]
        client.close()

    def test_get_snapshot(self, httpx_mock) -> None:
        """GET /v2/storage/snapshots/{id} returns the snapshot dict."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/snapshots/954",
            method="GET",
            json={**SNAPSHOT, "table": {"id": "in.c-foo.data"}},
            status_code=200,
        )

        client = self._client()
        snapshot = client.get_snapshot("954")
        client.close()

        assert snapshot["id"] == "954"
        assert snapshot["table"]["id"] == "in.c-foo.data"

    def test_delete_snapshot_sync_204(self, httpx_mock) -> None:
        """DELETE /v2/storage/snapshots/{id} with a 204 response does not poll."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/snapshots/954",
            method="DELETE",
            status_code=204,
        )

        client = self._client()
        client.delete_snapshot("954")
        client.close()

        assert len(httpx_mock.get_requests()) == 1

    def test_delete_snapshot_202_job_polled(self, httpx_mock) -> None:
        """Forward-compat: a 202 job response is polled to completion."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/snapshots/954",
            method="DELETE",
            json={"id": 888, "status": "waiting"},
            status_code=202,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/888",
            method="GET",
            json={"id": 888, "status": "success"},
            status_code=200,
        )

        client = self._client()
        client.delete_snapshot("954")
        client.close()

        assert len(httpx_mock.get_requests()) == 2

    def test_table_from_snapshot_posts_tables_async(self, httpx_mock) -> None:
        """POST buckets/{id}/tables-async with snapshotId+name; job polled to results.

        The restore goes through the classic tables-async import endpoint --
        NOT tables-definition -- matching the reference PHP client's
        createTableFromSnapshot.
        """
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/buckets/in.c-dest/tables-async",
            method="POST",
            json={"id": 999, "status": "waiting", "operationName": "tableCreate"},
            status_code=202,
        )
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/jobs/999",
            method="GET",
            json={
                "id": 999,
                "status": "success",
                "results": {"id": "in.c-dest.restored", "rowsCount": 3},
            },
            status_code=200,
        )

        client = self._client()
        results = client.create_table_from_snapshot(
            bucket_id="in.c-dest", snapshot_id="954", name="restored"
        )
        client.close()

        assert results["id"] == "in.c-dest.restored"
        request = httpx_mock.get_requests()[0]
        assert json.loads(request.content) == {"snapshotId": "954", "name": "restored"}

    def test_table_from_snapshot_branch_prefix(self, httpx_mock) -> None:
        """branch_id routes through /v2/storage/branch/{id}/buckets/.../tables-async."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/branch/42/buckets/in.c-dest/tables-async",
            method="POST",
            json={"id": 1, "status": "success", "results": {"id": "in.c-dest.restored"}},
            status_code=202,
        )

        client = self._client()
        results = client.create_table_from_snapshot(
            bucket_id="in.c-dest", snapshot_id="954", name="restored", branch_id=42
        )
        client.close()

        assert results["id"] == "in.c-dest.restored"

    def test_api_error_propagates(self, httpx_mock) -> None:
        """Storage API 4xx propagates as KeboolaApiError."""
        httpx_mock.add_response(
            url="https://connection.keboola.com/v2/storage/snapshots/999",
            method="GET",
            json={"error": "Snapshot 999 not found."},
            status_code=404,
        )

        client = self._client()
        with pytest.raises(KeboolaApiError):
            client.get_snapshot("999")
        client.close()


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


class TestSnapshotService:
    """Business-logic tests for SnapshotService."""

    def test_create_snapshot_returns_id(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table_snapshot.return_value = {"id": "954"}
        service = _make_service(store, mock_client)

        result = service.create_snapshot(
            alias="test", table_id="in.c-foo.data", description="d", branch_id=None
        )

        assert result["snapshot_id"] == "954"
        assert result["table_id"] == "in.c-foo.data"
        assert result["project_alias"] == "test"
        mock_client.create_table_snapshot.assert_called_once_with(
            table_id="in.c-foo.data", description="d", branch_id=None
        )
        mock_client.close.assert_called_once()

    def test_list_snapshots_counts(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.list_table_snapshots.return_value = [SNAPSHOT, {**SNAPSHOT, "id": "955"}]
        service = _make_service(store, mock_client)

        result = service.list_snapshots(alias="test", table_id="in.c-foo.data", limit=10)

        assert result["count"] == 2
        assert result["snapshots"][1]["id"] == "955"
        mock_client.list_table_snapshots.assert_called_once_with(
            table_id="in.c-foo.data", limit=10, branch_id=None
        )

    def test_get_snapshot_passthrough(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.get_snapshot.return_value = {**SNAPSHOT, "table": {"id": "in.c-foo.data"}}
        service = _make_service(store, mock_client)

        result = service.get_snapshot(alias="test", snapshot_id="954")

        assert result["snapshot"]["table"]["id"] == "in.c-foo.data"
        mock_client.get_snapshot.assert_called_once_with("954")

    def test_delete_snapshots_partial_failure(self, tmp_path: Path) -> None:
        """One failing delete does not block the rest (error accumulation)."""
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.delete_snapshot.side_effect = [
            KeboolaApiError(message="Snapshot 1 not found.", status_code=404),
            None,
        ]
        service = _make_service(store, mock_client)

        result = service.delete_snapshots(alias="test", snapshot_ids=["1", "2"])

        assert result["deleted"] == ["2"]
        assert result["failed"] == [{"id": "1", "error": "Snapshot 1 not found."}]
        mock_client.close.assert_called_once()

    def test_delete_snapshots_dry_run_makes_no_calls(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.delete_snapshots(alias="test", snapshot_ids=["1", "2"], dry_run=True)

        assert result["would_delete"] == ["1", "2"]
        assert result["deleted"] == []
        mock_client.delete_snapshot.assert_not_called()

    def test_table_from_snapshot_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table_from_snapshot.return_value = {
            "id": "in.c-dest.restored",
            "rowsCount": 3,
        }
        service = _make_service(store, mock_client)

        result = service.create_table_from_snapshot(
            alias="test",
            bucket_id="in.c-dest",
            snapshot_id="954",
            name="restored",
        )

        assert result["table_id"] == "in.c-dest.restored"
        assert result["dry_run"] is False
        mock_client.create_table_from_snapshot.assert_called_once_with(
            bucket_id="in.c-dest", snapshot_id="954", name="restored", branch_id=None
        )
        mock_client.close.assert_called_once()

    def test_table_from_snapshot_dry_run_makes_no_calls(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        service = _make_service(store, mock_client)

        result = service.create_table_from_snapshot(
            alias="test",
            bucket_id="in.c-dest",
            snapshot_id="954",
            name="restored",
            dry_run=True,
        )

        assert result["dry_run"] is True
        mock_client.create_table_from_snapshot.assert_not_called()

    @pytest.mark.parametrize(
        ("bucket_id", "snapshot_id", "name"),
        [
            ("", "954", "restored"),
            ("in.c-dest", " ", "restored"),
            ("in.c-dest", "954", ""),
        ],
    )
    def test_table_from_snapshot_rejects_blank_inputs(
        self, tmp_path: Path, bucket_id: str, snapshot_id: str, name: str
    ) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ConfigError):
            service.create_table_from_snapshot(
                alias="test", bucket_id=bucket_id, snapshot_id=snapshot_id, name=name
            )

    def test_unknown_project_raises_config_error(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        service = _make_service(store, MagicMock())

        with pytest.raises(ConfigError):
            service.list_snapshots(alias="nope", table_id="in.c-foo.data")

    def test_client_closed_when_create_raises(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        mock_client = MagicMock()
        mock_client.create_table_snapshot.side_effect = KeboolaApiError(
            message="boom", status_code=500
        )
        service = _make_service(store, mock_client)

        with pytest.raises(KeboolaApiError):
            service.create_snapshot(alias="test", table_id="in.c-foo.data")
        mock_client.close.assert_called_once()


# ---------------------------------------------------------------------------
# CLI layer
# ---------------------------------------------------------------------------


class TestSnapshotCLI:
    """CLI tests for the `kbagent storage snapshot*` / table-from-snapshot commands."""

    def _project_with_active_branch(self, store: ConfigStore, branch_id: int) -> None:
        config = store.load()
        config.projects["test"].active_branch_id = branch_id
        store.save(config)

    def test_snapshots_json_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_snapshots.return_value = {
                "project_alias": "test",
                "table_id": "in.c-foo.data",
                "branch_id": None,
                "count": 1,
                "snapshots": [SNAPSHOT],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "snapshots",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["count"] == 1
        assert data["snapshots"][0]["id"] == "954"
        svc.list_snapshots.assert_called_once_with(
            alias="test", table_id="in.c-foo.data", limit=None, branch_id=None
        )

    def test_snapshots_ignores_active_branch(self, tmp_path: Path) -> None:
        """Read command: implicit active dev branch is skipped (production endpoint)."""
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 42)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_snapshots.return_value = {
                "project_alias": "test",
                "table_id": "in.c-foo.data",
                "branch_id": None,
                "count": 0,
                "snapshots": [],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "snapshots",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                ],
            )

        assert result.exit_code == 0, result.output
        assert svc.list_snapshots.call_args.kwargs["branch_id"] is None

    def test_snapshot_create_uses_active_branch(self, tmp_path: Path) -> None:
        """Write command: the active dev branch set via `branch use` is honored."""
        store = _make_store(tmp_path)
        self._project_with_active_branch(store, 42)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_snapshot.return_value = {
                "project_alias": "test",
                "table_id": "in.c-foo.data",
                "branch_id": 42,
                "snapshot_id": "954",
                "description": None,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "snapshot-create",
                    "--project",
                    "test",
                    "--table-id",
                    "in.c-foo.data",
                ],
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["snapshot_id"] == "954"
        assert svc.create_snapshot.call_args.kwargs["branch_id"] == 42

    def test_snapshot_detail_json(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.get_snapshot.return_value = {
                "project_alias": "test",
                "snapshot": {**SNAPSHOT, "table": {"id": "in.c-foo.data"}},
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "snapshot-detail",
                    "--project",
                    "test",
                    "--snapshot-id",
                    "954",
                ],
            )

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["snapshot"]["table"]["id"] == "in.c-foo.data"

    def test_snapshot_delete_failed_exits_1(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.delete_snapshots.return_value = {
                "project_alias": "test",
                "deleted": [],
                "failed": [{"id": "954", "error": "Snapshot 954 not found."}],
                "dry_run": False,
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "snapshot-delete",
                    "--project",
                    "test",
                    "--snapshot-id",
                    "954",
                ],
            )

        assert result.exit_code == 1, result.output

    def test_snapshot_delete_confirm_abort(self, tmp_path: Path) -> None:
        """Human mode without --yes prompts; answering 'n' aborts with exit 0."""
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            result = runner.invoke(
                app,
                ["storage", "snapshot-delete", "--project", "test", "--snapshot-id", "954"],
                input="n\n",
            )

        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        svc.delete_snapshots.assert_not_called()

    def test_table_from_snapshot_json_happy_path(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table_from_snapshot.return_value = {
                "project_alias": "test",
                "bucket_id": "in.c-dest",
                "snapshot_id": "954",
                "name": "restored",
                "branch_id": None,
                "dry_run": False,
                "table": {"id": "in.c-dest.restored", "rowsCount": 3},
                "table_id": "in.c-dest.restored",
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "table-from-snapshot",
                    "--project",
                    "test",
                    "--snapshot-id",
                    "954",
                    "--bucket-id",
                    "in.c-dest",
                    "--name",
                    "restored",
                ],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["table_id"] == "in.c-dest.restored"
        svc.create_table_from_snapshot.assert_called_once_with(
            alias="test",
            bucket_id="in.c-dest",
            snapshot_id="954",
            name="restored",
            branch_id=None,
            dry_run=False,
        )

    def test_table_from_snapshot_requires_name(self, tmp_path: Path) -> None:
        """--name is required (the live API rejects an empty name)."""
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService"),
        ):
            MockStore.return_value = store
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "table-from-snapshot",
                    "--project",
                    "test",
                    "--snapshot-id",
                    "954",
                    "--bucket-id",
                    "in.c-dest",
                ],
            )

        assert result.exit_code == 2, result.output

    def test_table_from_snapshot_config_error_exits_5(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.SnapshotService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.create_table_from_snapshot.side_effect = ConfigError("unknown project")
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "table-from-snapshot",
                    "--project",
                    "nope",
                    "--snapshot-id",
                    "954",
                    "--bucket-id",
                    "in.c-dest",
                    "--name",
                    "restored",
                ],
            )

        assert result.exit_code == 5, result.output
