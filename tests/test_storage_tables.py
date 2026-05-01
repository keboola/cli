"""Tests for storage tables multi-project listing (issue #198).

Covers:
- StorageService.list_tables() multi-project parallel execution
- CLI storage tables without --project (all projects)
- CLI storage tables with multiple --project flags
- Error accumulation across projects
- --branch/--project validation (branch requires single project)
- --bucket-id filter applied independently per project
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.storage_service import StorageService

runner = CliRunner()

TEST_TOKEN = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"


def _make_multi_store(tmp_path: Path) -> ConfigStore:
    """Config store with two projects ('p1' and 'p2')."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    store = ConfigStore(config_dir=config_dir)
    config = AppConfig(
        projects={
            "p1": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            ),
            "p2": ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=TEST_TOKEN,
            ),
        },
    )
    store.save(config)
    return store


def _make_single_store(tmp_path: Path) -> ConfigStore:
    """Config store with a single project ('test')."""
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


def _mk_table(table_id: str) -> dict:
    """Helper to build a minimal raw API table dict."""
    return {
        "id": table_id,
        "name": table_id.split(".")[-1],
        "displayName": table_id.split(".")[-1],
        "bucket": {"id": ".".join(table_id.split(".")[:2])},
        "rowsCount": 100,
        "dataSizeBytes": 1024,
        "isAlias": False,
        "lastImportDate": "2026-04-22T00:00:00+0000",
    }


# ------------------------------------------------------------------
# Service-layer tests
# ------------------------------------------------------------------


class TestListTablesMultiProject:
    """StorageService.list_tables() with multiple project aliases."""

    def test_two_projects_aggregate_tables(self, tmp_path: Path) -> None:
        """aliases=['p1','p2'] returns tables from both with project_alias set."""
        store = _make_multi_store(tmp_path)

        p1_client = MagicMock()
        p1_client.list_tables.return_value = [_mk_table("in.c-a.t1")]
        p2_client = MagicMock()
        p2_client.list_tables.return_value = [_mk_table("in.c-b.t2")]

        # Route client_factory by token to return the right mock; both projects
        # share the same token in the fixture, so use stack_url for routing
        # would not work either. Instead, dispatch by call order using a
        # rotating list -- _run_parallel spawns threads so ordering may vary,
        # so we use a dict keyed by call count and assert set-wise below.
        clients: dict[int, MagicMock] = {0: p1_client, 1: p2_client}
        call_count = {"n": 0}

        def factory(url: str, token: str) -> MagicMock:
            idx = call_count["n"]
            call_count["n"] += 1
            return clients[idx]

        service = StorageService(config_store=store, client_factory=factory)

        result = service.list_tables(aliases=["p1", "p2"])

        assert len(result["tables"]) == 2
        assert result["errors"] == []
        aliases_seen = {t["project_alias"] for t in result["tables"]}
        assert aliases_seen == {"p1", "p2"}
        ids_seen = {t["id"] for t in result["tables"]}
        assert ids_seen == {"in.c-a.t1", "in.c-b.t2"}

    def test_aliases_none_queries_all_projects(self, tmp_path: Path) -> None:
        """aliases=None resolves to all projects."""
        store = _make_multi_store(tmp_path)

        mock_client = MagicMock()
        mock_client.list_tables.return_value = [_mk_table("in.c-b.t")]

        service = StorageService(
            config_store=store,
            client_factory=lambda _u, _t: mock_client,
        )

        result = service.list_tables(aliases=None)

        # Both p1 and p2 hit the client once each (same mock)
        assert mock_client.list_tables.call_count == 2
        assert len(result["tables"]) == 2
        assert {t["project_alias"] for t in result["tables"]} == {"p1", "p2"}

    def test_error_accumulation_partial_success(self, tmp_path: Path) -> None:
        """One project 404s; other succeeds. Errors list captures the failure."""
        store = _make_multi_store(tmp_path)

        good_client = MagicMock()
        good_client.list_tables.return_value = [_mk_table("in.c-x.ok")]

        bad_client = MagicMock()
        bad_client.list_tables.side_effect = KeboolaApiError(
            message="Bucket not found",
            error_code="NOT_FOUND",
            status_code=404,
        )

        clients = [good_client, bad_client]
        call_count = {"n": 0}

        def factory(url: str, token: str) -> MagicMock:
            idx = call_count["n"]
            call_count["n"] += 1
            return clients[idx]

        service = StorageService(config_store=store, client_factory=factory)

        result = service.list_tables(aliases=["p1", "p2"], bucket_id="in.c-missing")

        # Exactly one project returned tables, one produced an error
        assert len(result["tables"]) == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error_code"] == "NOT_FOUND"
        assert result["errors"][0]["project_alias"] in {"p1", "p2"}

    def test_bucket_id_filter_applied_per_project(self, tmp_path: Path) -> None:
        """bucket_id is forwarded to each per-project client call."""
        store = _make_multi_store(tmp_path)

        mock_client = MagicMock()
        mock_client.list_tables.return_value = []
        service = StorageService(
            config_store=store,
            client_factory=lambda _u, _t: mock_client,
        )

        service.list_tables(aliases=["p1", "p2"], bucket_id="in.c-shared")

        assert mock_client.list_tables.call_count == 2
        for call in mock_client.list_tables.call_args_list:
            assert call.kwargs == {"bucket_id": "in.c-shared", "branch_id": None}

    def test_unknown_alias_raises_config_error(self, tmp_path: Path) -> None:
        """Passing an unknown alias raises ConfigError (from resolve_projects)."""
        store = _make_multi_store(tmp_path)
        service = StorageService(
            config_store=store,
            client_factory=lambda _u, _t: MagicMock(),
        )

        with pytest.raises(ConfigError):
            service.list_tables(aliases=["does-not-exist"])


class TestStorageNullNumericCoercion:
    """Companion to issue #233: API may return null for rowsCount /
    dataSizeBytes on empty tables and buckets. Service layer must surface
    0, not null, so downstream JSON consumers see well-typed numbers.
    """

    def test_list_tables_coerces_null_numeric_fields_to_zero(self, tmp_path: Path) -> None:
        store = _make_single_store(tmp_path)

        mock_client = MagicMock()
        mock_client.list_tables.return_value = [
            {
                "id": "in.c-empty.t",
                "name": "t",
                "displayName": "t",
                "bucket": {"id": "in.c-empty"},
                "rowsCount": None,
                "dataSizeBytes": None,
                "isAlias": False,
                "lastImportDate": None,
            }
        ]
        service = StorageService(
            config_store=store,
            client_factory=lambda _u, _t: mock_client,
        )

        result = service.list_tables(aliases=["test"])

        assert len(result["tables"]) == 1
        t = result["tables"][0]
        assert t["rows_count"] == 0
        assert t["data_size_bytes"] == 0

    def test_list_buckets_coerces_null_numeric_fields_to_zero(self, tmp_path: Path) -> None:
        store = _make_single_store(tmp_path)

        mock_client = MagicMock()
        mock_client.list_buckets.return_value = [
            {
                "id": "in.c-empty",
                "displayName": "empty",
                "name": "c-empty",
                "stage": "in",
                "backend": "snowflake",
                "rowsCount": None,
                "dataSizeBytes": None,
                "description": "",
            }
        ]
        service = StorageService(
            config_store=store,
            client_factory=lambda _u, _t: mock_client,
        )

        result = service.list_buckets(aliases=["test"])

        assert len(result["buckets"]) == 1
        b = result["buckets"][0]
        assert b["rows_count"] == 0
        assert b["data_size_bytes"] == 0


# ------------------------------------------------------------------
# CLI-layer tests
# ------------------------------------------------------------------


class TestStorageTablesCli:
    """CLI tests for `kbagent storage tables`."""

    def test_no_project_queries_all(self, tmp_path: Path) -> None:
        """Omitting --project passes aliases=None to the service."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {"tables": [], "errors": []}
            result = runner.invoke(app, ["--json", "storage", "tables"])

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_tables.call_args.kwargs
        # Typer delivers a list[str] | None -- when nothing is passed it's None
        assert call_kwargs["aliases"] is None

        payload = json.loads(result.output)
        assert payload["data"] == {"tables": [], "errors": []}

    def test_multi_project_flags(self, tmp_path: Path) -> None:
        """Two --project flags are delivered as a list to the service."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {
                "tables": [
                    {
                        "project_alias": "p1",
                        "id": "in.c-a.t1",
                        "name": "t1",
                        "display_name": "t1",
                        "bucket_id": "in.c-a",
                        "rows_count": 10,
                        "data_size_bytes": 100,
                        "is_alias": False,
                        "last_import_date": "",
                    },
                    {
                        "project_alias": "p2",
                        "id": "in.c-b.t2",
                        "name": "t2",
                        "display_name": "t2",
                        "bucket_id": "in.c-b",
                        "rows_count": 20,
                        "data_size_bytes": 200,
                        "is_alias": False,
                        "last_import_date": "",
                    },
                ],
                "errors": [],
            }
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "tables",
                    "--project",
                    "p1",
                    "--project",
                    "p2",
                ],
            )

        assert result.exit_code == 0, result.output
        call_kwargs = svc.list_tables.call_args.kwargs
        assert call_kwargs["aliases"] == ["p1", "p2"]
        payload = json.loads(result.output)
        ids = {t["id"] for t in payload["data"]["tables"]}
        assert ids == {"in.c-a.t1", "in.c-b.t2"}

    def test_single_project_with_bucket_id(self, tmp_path: Path) -> None:
        """--project + --bucket-id narrows to a single project and forwards filter."""
        store = _make_single_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {"tables": [], "errors": []}
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "tables",
                    "--project",
                    "test",
                    "--bucket-id",
                    "in.c-data",
                ],
            )

        assert result.exit_code == 0
        call_kwargs = svc.list_tables.call_args.kwargs
        assert call_kwargs["aliases"] == ["test"]
        assert call_kwargs["bucket_id"] == "in.c-data"

    def test_multi_project_with_branch_rejected(self, tmp_path: Path) -> None:
        """--branch with two --project flags fails with exit code 2."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_tables.return_value = {"tables": [], "errors": []}
            result = runner.invoke(
                app,
                [
                    "--json",
                    "storage",
                    "tables",
                    "--project",
                    "p1",
                    "--project",
                    "p2",
                    "--branch",
                    "99",
                ],
            )

        # Per CONTRIBUTING.md exit code 2 = usage/argument validation
        assert result.exit_code == 2, result.output
        assert "--branch requires exactly one --project" in result.output

    def test_branch_without_project_rejected(self, tmp_path: Path) -> None:
        """--branch with no --project is also a usage error."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_tables.return_value = {"tables": [], "errors": []}
            result = runner.invoke(
                app,
                ["--json", "storage", "tables", "--branch", "99"],
            )

        assert result.exit_code == 2, result.output

    def test_errors_surface_in_json_output(self, tmp_path: Path) -> None:
        """Per-project errors are preserved verbatim in JSON mode."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            svc = MockSvc.return_value
            svc.list_tables.return_value = {
                "tables": [],
                "errors": [
                    {
                        "project_alias": "p1",
                        "error_code": "NOT_FOUND",
                        "message": "Bucket not found",
                    }
                ],
            }
            result = runner.invoke(app, ["--json", "storage", "tables"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["data"]["errors"][0]["project_alias"] == "p1"
        assert payload["data"]["errors"][0]["error_code"] == "NOT_FOUND"

    def test_unknown_project_exits_with_config_error(self, tmp_path: Path) -> None:
        """Service ConfigError is mapped to exit code 5."""
        store = _make_multi_store(tmp_path)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.StorageService") as MockSvc,
        ):
            MockStore.return_value = store
            MockSvc.return_value.list_tables.side_effect = ConfigError("Project 'ghost' not found.")
            result = runner.invoke(
                app,
                ["--json", "storage", "tables", "--project", "ghost"],
            )

        assert result.exit_code == 5
