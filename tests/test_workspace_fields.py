"""Tests for the creator and lifecycle fields of workspace list / detail."""

from pathlib import Path
from unittest.mock import MagicMock

from rich.console import Console

from helpers import setup_single_project
from keboola_agent_cli.output import format_workspaces_table
from keboola_agent_cli.services._workspace_fields import workspace_provenance_fields
from keboola_agent_cli.services.workspace_service import WorkspaceService

RAW_WORKSPACE = {
    "id": 42,
    "name": "WORKSPACE_42",
    "component": None,
    "configurationId": None,
    "connection": {"backend": "bigquery", "schema": "WORKSPACE_42", "loginType": "default"},
    "readOnlyStorageAccess": True,
    "created": "2026-07-08T13:06:00+0200",
    "creatorToken": {"id": 771, "description": "local-mcp-token"},
    "actor": {"type": "token", "id": "771", "name": "local-mcp-token", "adminId": None},
    "state": "active",
    "lastActivityAt": "2026-07-09T08:00:00+0200",
    "warnAt": "2027-01-09T08:00:00+0100",
    "deactivateAt": None,
    "deleteAt": None,
}

EXPECTED = {
    "creator_token_id": 771,
    "creator_token_description": "local-mcp-token",
    "actor": {"type": "token", "id": "771", "name": "local-mcp-token", "adminId": None},
    "last_activity_at": "2026-07-09T08:00:00+0200",
    "state": "active",
    "warn_at": "2027-01-09T08:00:00+0100",
    "deactivate_at": None,
    "delete_at": None,
}


def _service(tmp_config_dir: Path, client: MagicMock) -> WorkspaceService:
    client.list_dev_branches.return_value = [{"id": 1, "isDefault": True}]
    store = setup_single_project(tmp_config_dir)
    return WorkspaceService(config_store=store, client_factory=lambda url, token: client)


def test_fields_are_passed_through() -> None:
    assert workspace_provenance_fields(RAW_WORKSPACE) == EXPECTED


def test_missing_keys_are_none() -> None:
    """A stack that does not return the keys yields None, never a KeyError."""
    fields = workspace_provenance_fields({"id": 1, "creatorToken": None})
    assert set(fields) == set(EXPECTED)
    assert all(value is None for value in fields.values())


def test_list_workspaces_carries_the_fields(tmp_config_dir: Path) -> None:
    client = MagicMock()
    client.list_workspaces.return_value = [RAW_WORKSPACE]

    result = _service(tmp_config_dir, client).list_workspaces(aliases=["prod"])

    entry = result["workspaces"][0]
    assert {key: entry[key] for key in EXPECTED} == EXPECTED
    assert entry["created"] == "2026-07-08T13:06:00+0200"


def test_get_workspace_carries_the_fields(tmp_config_dir: Path) -> None:
    client = MagicMock()
    client.get_workspace.return_value = RAW_WORKSPACE

    result = _service(tmp_config_dir, client).get_workspace(alias="prod", workspace_id=42)

    assert {key: result[key] for key in EXPECTED} == EXPECTED


def test_table_shows_token_and_last_activity() -> None:
    console = Console(record=True, width=250)
    entry = {"project_alias": "prod", "id": 42, "name": "WORKSPACE_42", **EXPECTED}

    format_workspaces_table(console, {"workspaces": [entry], "errors": []})

    text = console.export_text()
    assert "Token" in text
    assert "Last activity" in text
    assert "local-mcp-token" in text
    assert "2026-07-09T08:00:00+0200" in text
