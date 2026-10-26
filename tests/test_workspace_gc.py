"""Tests for workspace --orphaned flag and workspace gc command.

Covers:
  - workspace list --orphaned (service-level filtering + CLI)
  - workspace gc --dry-run (preview without deletion)
  - workspace gc (delete orphans, error accumulation)
  - _is_orphaned_workspace helper
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.workspace_service import WorkspaceService, _is_orphaned_workspace

runner = CliRunner()

TEST_TOKEN = "test-token-456"
TEST_URL = "https://connection.keboola.com"


def _setup_store(tmp_path: Path) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url=TEST_URL,
            token=TEST_TOKEN,
            project_name="Prod",
            project_id=1,
        ),
    )
    return store


def _invoke(store: ConfigStore, mock_ws_svc: MagicMock, *args: str, input: str | None = None):
    with (
        patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
        patch("keboola_agent_cli.cli.ProjectService") as MockProjSvc,
        patch("keboola_agent_cli.cli.ConfigService") as MockCfgSvc,
        patch("keboola_agent_cli.cli.JobService") as MockJobSvc,
        patch("keboola_agent_cli.cli.WorkspaceService") as MockWsSvc,
    ):
        MockStore.return_value = store
        MockProjSvc.return_value = ProjectService(config_store=store)
        MockCfgSvc.return_value = ConfigService(config_store=store)
        MockJobSvc.return_value = JobService(config_store=store)
        MockWsSvc.return_value = mock_ws_svc
        return runner.invoke(app, list(args), input=input)


# ── _is_orphaned_workspace unit tests ─────────────────────────────────


class TestIsOrphanedWorkspace:
    def test_non_sandboxes_component_never_orphan(self) -> None:
        ws = {"component_id": "keboola.snowflake-transformation", "config_id": "cfg-1"}
        assert not _is_orphaned_workspace(ws, {"cfg-1": "My Config"})

    def test_sandboxes_with_existing_config_not_orphan(self) -> None:
        ws = {"component_id": "keboola.sandboxes", "config_id": "cfg-1"}
        assert not _is_orphaned_workspace(ws, {"cfg-1": "My Workspace"})

    def test_sandboxes_with_missing_config_is_orphan(self) -> None:
        ws = {"component_id": "keboola.sandboxes", "config_id": "cfg-missing"}
        assert _is_orphaned_workspace(ws, {"cfg-other": "Other"})

    def test_sandboxes_with_empty_config_id_is_orphan(self) -> None:
        ws = {"component_id": "keboola.sandboxes", "config_id": ""}
        assert _is_orphaned_workspace(ws, {"cfg-1": "My Workspace"})

    def test_sandboxes_with_no_config_id_key_is_orphan(self) -> None:
        ws = {"component_id": "keboola.sandboxes"}
        assert _is_orphaned_workspace(ws, {"cfg-1": "My Workspace"})


# ── workspace list --orphaned CLI tests ───────────────────────────────


class TestWorkspaceListOrphaned:
    def test_list_orphaned_json(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        orphan_ws = {
            "project_alias": "prod",
            "id": 99,
            "name": "orphan-ws",
            "backend": "snowflake",
            "host": "host.snowflake.com",
            "schema": "WORKSPACE_99",
            "user": "u",
            "created": "2025-01-01",
            "component_id": "keboola.sandboxes",
            "config_id": "",
        }
        mock_ws.list_workspaces.return_value = {
            "workspaces": [orphan_ws],
            "errors": [],
        }
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "list",
            "--project",
            "prod",
            "--orphaned",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["data"]["workspaces"]) == 1
        # Verify service was called with orphaned_only=True
        mock_ws.list_workspaces.assert_called_once_with(
            aliases=["prod"],
            orphaned_only=True,
            branch_id=None,
            qs_compatible_only=False,
        )

    def test_list_without_orphaned_flag(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.list_workspaces.return_value = {"workspaces": [], "errors": []}
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "list",
            "--project",
            "prod",
        )
        assert result.exit_code == 0, result.output
        # orphaned_only=False (default) when flag absent
        mock_ws.list_workspaces.assert_called_once_with(
            aliases=["prod"],
            orphaned_only=False,
            branch_id=None,
            qs_compatible_only=False,
        )


# ── workspace gc CLI tests ─────────────────────────────────────────────


class TestWorkspaceGc:
    def test_gc_dry_run_json(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.gc_workspaces.return_value = {
            "dry_run": True,
            "would_delete": [{"id": 5, "project_alias": "prod", "name": "orphan"}],
            "count": 1,
            "errors": [],
            "message": "DRY RUN: 1 orphaned workspace(s) would be deleted.",
        }
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "gc",
            "--project",
            "prod",
            "--dry-run",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["dry_run"] is True
        assert data["data"]["count"] == 1
        mock_ws.gc_workspaces.assert_called_once_with(aliases=["prod"], dry_run=True)

    def test_gc_delete_with_yes_flag(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.gc_workspaces.return_value = {
            "dry_run": False,
            "deleted": [{"id": 5, "project_alias": "prod"}],
            "errors": [],
            "count_deleted": 1,
            "count_errors": 0,
            "message": "GC complete: 1 orphaned workspace(s) deleted.",
        }
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "gc",
            "--project",
            "prod",
            "--yes",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["count_deleted"] == 1
        assert data["data"]["dry_run"] is False

    @pytest.mark.usefixtures("interactive_terminal")
    def test_gc_no_confirmation_aborts(self, tmp_path: Path) -> None:
        """In non-JSON mode without --yes, answering 'n' exits 0 without calling service."""
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        result = _invoke(
            store,
            mock_ws,
            "workspace",
            "gc",
            "--project",
            "prod",
            input="n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output
        mock_ws.gc_workspaces.assert_not_called()

    def test_gc_nothing_to_delete(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.gc_workspaces.return_value = {
            "dry_run": False,
            "deleted": [],
            "errors": [],
            "count_deleted": 0,
            "count_errors": 0,
            "message": "GC complete: 0 orphaned workspace(s) deleted.",
        }
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "gc",
            "--project",
            "prod",
            "--yes",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["count_deleted"] == 0

    def test_gc_without_target_cleans_only_the_active_project(self, tmp_path: Path) -> None:
        """A bare `workspace gc` cleans up the active project, never every project (T3, T17)."""
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.gc_workspaces.return_value = {
            "dry_run": False,
            "deleted": [],
            "errors": [],
            "count_deleted": 0,
            "count_errors": 0,
            "message": "GC complete: 0 orphaned workspace(s) deleted.",
        }
        result = _invoke(store, mock_ws, "--json", "workspace", "gc", "--yes")
        assert result.exit_code == 0, result.output
        assert mock_ws.gc_workspaces.call_args.kwargs["aliases"] == ["prod"]

    def test_gc_project_with_all_projects_is_usage_error(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        result = _invoke(
            store,
            mock_ws,
            "--json",
            "workspace",
            "gc",
            "--project",
            "prod",
            "--all-projects",
            "--yes",
        )
        assert result.exit_code == 2, result.output
        assert json.loads(result.output)["error"]["code"] == "USAGE_ERROR"
        mock_ws.gc_workspaces.assert_not_called()

    def test_gc_all_projects_selects_every_project(self, tmp_path: Path) -> None:
        store = _setup_store(tmp_path)
        mock_ws = MagicMock()
        mock_ws.resolve_projects.return_value = {}
        mock_ws.gc_workspaces.return_value = {
            "dry_run": True,
            "would_delete": [],
            "count": 0,
            "errors": [],
            "message": "DRY RUN: 0 orphaned workspace(s) would be deleted.",
        }
        result = _invoke(store, mock_ws, "--json", "workspace", "gc", "--all-projects", "--dry-run")
        assert result.exit_code == 0, result.output
        mock_ws.gc_workspaces.assert_called_once_with(aliases=None, dry_run=True)


# ── WorkspaceService.gc_workspaces unit tests ──────────────────────────


# Branch component listings that confirm "no keboola.sandboxes configs".
_CONFIRMED_ABSENT = pytest.mark.parametrize(
    "confirm",
    [
        [],
        [{"id": "keboola.ex-http", "configurations": [{"id": "1"}]}],
        [{"id": "keboola.sandboxes", "configurations": []}],
    ],
    ids=["no-components", "other-component", "sandboxes-empty"],
)

# Answers that do not confirm it: the check fails, or sandbox configs exist.
_NOT_CONFIRMED = pytest.mark.parametrize(
    "confirm",
    [
        KeboolaApiError(
            message="Branch not found", status_code=404, error_code="NOT_FOUND", retryable=False
        ),
        RuntimeError("connection reset"),
        [{"id": "keboola.sandboxes", "configurations": [{"id": "live-cfg"}]}],
        [{"id": "keboola.sandboxes"}],
    ],
    ids=["check-404", "check-error", "sandboxes-has-configs", "sandboxes-no-key"],
)


class TestWorkspaceServiceGc:
    """Test gc_workspaces service method with a mocked client."""

    def _make_service(self, tmp_path: Path) -> tuple[WorkspaceService, MagicMock]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(stack_url=TEST_URL, token=TEST_TOKEN, project_name="Prod", project_id=1),
        )
        mock_client = MagicMock()
        mock_client.close = MagicMock()
        svc = WorkspaceService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        return svc, mock_client

    def _setup_client_for_list(
        self,
        mock_client: MagicMock,
        raw_workspaces: list[dict],
        sandbox_configs: list[dict],
    ) -> None:
        # list_dev_branches → branch_id
        mock_client.list_dev_branches.return_value = [{"id": 100, "isDefault": True}]
        mock_client.list_workspaces.return_value = raw_workspaces
        mock_client.list_component_configs.return_value = sandbox_configs

    def test_gc_dry_run_returns_orphans_without_deleting(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        orphan_ws = {
            "id": 77,
            "name": "WORKSPACE_77",
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(
            mock_client,
            raw_workspaces=[orphan_ws],
            sandbox_configs=[],  # no sandbox configs → orphan
        )
        result = svc.gc_workspaces(aliases=["prod"], dry_run=True)
        assert result["dry_run"] is True
        assert result["count"] == 1
        assert result["would_delete"][0]["id"] == 77
        # delete_workspace should NOT have been called
        mock_client.delete_workspace.assert_not_called()

    def test_gc_deletes_orphan_and_sandbox_config(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        orphan_ws = {
            "id": 99,
            "name": "WORKSPACE_99",
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(
            mock_client,
            raw_workspaces=[orphan_ws],
            sandbox_configs=[],
        )
        # delete_workspace in service also calls get_workspace then delete
        mock_client.get_workspace.return_value = {
            "id": 99,
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {},
        }
        mock_client.delete_workspace.return_value = None
        mock_client.delete_config.return_value = None

        result = svc.gc_workspaces(aliases=["prod"], dry_run=False)
        assert result["dry_run"] is False
        assert result["count_deleted"] == 1
        assert result["count_errors"] == 0
        mock_client.delete_workspace.assert_called_once_with(99, branch_id=100)
        mock_client.delete_config.assert_called_once_with(
            "keboola.sandboxes", "orphan-cfg", branch_id=100
        )

    def test_gc_skips_non_sandbox_workspaces(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        # Transformation workspace — should NOT be considered orphaned
        tfm_ws = {
            "id": 55,
            "name": "WORKSPACE_55",
            "component": "keboola.snowflake-transformation",
            "configurationId": "tfm-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(
            mock_client,
            raw_workspaces=[tfm_ws],
            sandbox_configs=[],
        )
        result = svc.gc_workspaces(aliases=["prod"], dry_run=True)
        assert result["count"] == 0
        assert result["would_delete"] == []

    def test_gc_delete_error_accumulated(self, tmp_path: Path) -> None:
        svc, mock_client = self._make_service(tmp_path)
        orphan_ws = {
            "id": 77,
            "name": "WORKSPACE_77",
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(
            mock_client,
            raw_workspaces=[orphan_ws],
            sandbox_configs=[],
        )
        mock_client.get_workspace.return_value = {
            "id": 77,
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {},
        }
        mock_client.delete_workspace.side_effect = KeboolaApiError(
            message="Delete failed", status_code=500, error_code="INTERNAL_ERROR", retryable=True
        )
        result = svc.gc_workspaces(aliases=["prod"], dry_run=False)
        # Error should be accumulated, not raised
        assert result["count_deleted"] == 0
        assert result["count_errors"] == 1

    def test_gc_failed_config_listing_deletes_nothing(self, tmp_path: Path) -> None:
        """A project whose sandbox configs cannot be listed goes to errors[].

        Before the fix the failure was swallowed into an empty mapping, so every
        keboola.sandboxes workspace counted as an orphan and was deleted.
        """
        svc, mock_client = self._make_service(tmp_path)
        live_ws = {
            "id": 88,
            "name": "WORKSPACE_88",
            "component": "keboola.sandboxes",
            "configurationId": "live-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(mock_client, raw_workspaces=[live_ws], sandbox_configs=[])
        mock_client.list_component_configs.side_effect = KeboolaApiError(
            message="Service unavailable", status_code=503, error_code="API_ERROR", retryable=True
        )
        result = svc.gc_workspaces(aliases=["prod"], dry_run=False)
        assert result["count_deleted"] == 0
        assert result["errors"] == [
            {"project_alias": "prod", "error_code": "API_ERROR", "message": "Service unavailable"}
        ]
        mock_client.delete_workspace.assert_not_called()
        mock_client.delete_config.assert_not_called()

    def _setup_not_found_listing(self, mock_client: MagicMock, confirm: Any) -> None:
        """One orphan; the sandbox config listing 404s; ``confirm`` answers the check."""
        orphan_ws = {
            "id": 77,
            "name": "WORKSPACE_77",
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {"backend": "snowflake"},
            "created": "2025-01-01",
        }
        self._setup_client_for_list(mock_client, raw_workspaces=[orphan_ws], sandbox_configs=[])
        mock_client.list_component_configs.side_effect = KeboolaApiError(
            message="Resource not found", status_code=404, error_code="NOT_FOUND", retryable=False
        )
        if isinstance(confirm, Exception):
            mock_client.list_components_summary.side_effect = confirm
        else:
            mock_client.list_components_summary.return_value = confirm
        mock_client.get_workspace.return_value = {
            "id": 77,
            "component": "keboola.sandboxes",
            "configurationId": "orphan-cfg",
            "connection": {},
        }

    @_CONFIRMED_ABSENT
    def test_gc_not_found_confirmed_absent_deletes_orphan(
        self, tmp_path: Path, confirm: list[dict]
    ) -> None:
        """A 404 plus a branch listing without sandbox configs means "no configs"."""
        svc, mock_client = self._make_service(tmp_path)
        self._setup_not_found_listing(mock_client, confirm)
        result = svc.gc_workspaces(aliases=["prod"], dry_run=False)
        assert result["errors"] == []
        assert result["count_deleted"] == 1
        mock_client.list_components_summary.assert_called_once_with(branch_id=100)
        mock_client.delete_workspace.assert_called_once_with(77, branch_id=100)

    @_NOT_CONFIRMED
    def test_gc_not_found_unconfirmed_deletes_nothing(self, tmp_path: Path, confirm: Any) -> None:
        """A 404 alone (deleted branch, routing problem) never makes orphans."""
        svc, mock_client = self._make_service(tmp_path)
        self._setup_not_found_listing(mock_client, confirm)
        result = svc.gc_workspaces(aliases=["prod"], dry_run=False)
        assert result["count_deleted"] == 0
        assert result["errors"] == [
            {"project_alias": "prod", "error_code": "NOT_FOUND", "message": "Resource not found"}
        ]
        mock_client.delete_workspace.assert_not_called()
        mock_client.delete_config.assert_not_called()

    @_CONFIRMED_ABSENT
    def test_list_orphaned_not_found_confirmed_absent_lists_orphan(
        self, tmp_path: Path, confirm: list[dict]
    ) -> None:
        svc, mock_client = self._make_service(tmp_path)
        self._setup_not_found_listing(mock_client, confirm)
        result = svc.list_workspaces(aliases=["prod"], orphaned_only=True)
        assert result["errors"] == []
        assert [ws["id"] for ws in result["workspaces"]] == [77]

    @_NOT_CONFIRMED
    def test_list_orphaned_not_found_unconfirmed_reports_error(
        self, tmp_path: Path, confirm: Any
    ) -> None:
        svc, mock_client = self._make_service(tmp_path)
        self._setup_not_found_listing(mock_client, confirm)
        result = svc.list_workspaces(aliases=["prod"], orphaned_only=True)
        assert result["workspaces"] == []
        assert result["errors"][0]["error_code"] == "NOT_FOUND"

    def test_list_orphaned_failed_config_listing_reports_error(self, tmp_path: Path) -> None:
        """`workspace list --orphaned` shares the rule: no false orphans."""
        svc, mock_client = self._make_service(tmp_path)
        live_ws = {
            "id": 88,
            "component": "keboola.sandboxes",
            "configurationId": "live-cfg",
            "connection": {},
        }
        self._setup_client_for_list(mock_client, raw_workspaces=[live_ws], sandbox_configs=[])
        mock_client.list_component_configs.side_effect = RuntimeError("boom")
        result = svc.list_workspaces(aliases=["prod"], orphaned_only=True)
        assert result["workspaces"] == []
        assert result["errors"][0]["project_alias"] == "prod"

    def test_list_without_orphaned_keeps_name_fallback(self, tmp_path: Path) -> None:
        """A plain listing still falls back to internal names on a failed config listing."""
        svc, mock_client = self._make_service(tmp_path)
        live_ws = {
            "id": 88,
            "name": "WORKSPACE_88",
            "component": "keboola.sandboxes",
            "configurationId": "live-cfg",
            "connection": {},
        }
        self._setup_client_for_list(mock_client, raw_workspaces=[live_ws], sandbox_configs=[])
        mock_client.list_component_configs.side_effect = RuntimeError("boom")
        result = svc.list_workspaces(aliases=["prod"])
        assert result["errors"] == []
        assert result["workspaces"][0]["name"] == "WORKSPACE_88"
