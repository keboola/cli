"""Tests for ConfigService.create_config_row, update_config_row, and get_oauth_url."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.services.config_service import ConfigService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_ROW = {
    "id": "row-001",
    "name": "My Row",
    "description": "",
    "configuration": {
        "parameters": {
            "table": "orders",
            "limit": 1000,
        }
    },
    "isDisabled": False,
}


def _make_service(tmp_config_dir: Path) -> tuple[ConfigService, MagicMock]:
    """Create a ConfigService backed by a mock client."""
    store = setup_single_project(tmp_config_dir)
    mock_client = MagicMock()
    mock_client.get_config_row.return_value = SAMPLE_ROW
    mock_client.create_config_row.return_value = {**SAMPLE_ROW, "id": "row-new"}
    mock_client.update_config_row.return_value = {**SAMPLE_ROW, "name": "Updated Row"}
    mock_client.get_oauth_url.return_value = (
        "https://external.keboola.com/oauth/index.html"
        "?token=abc123&sapiUrl=https%3A%2F%2Fconnection.keboola.com"
        "#/keboola.ex-google-drive/cfg-001"
    )
    service = ConfigService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


# ---------------------------------------------------------------------------
# create_config_row tests
# ---------------------------------------------------------------------------


class TestCreateConfigRow:
    """Tests for ConfigService.create_config_row."""

    def test_create_minimal(self, tmp_config_dir: Path) -> None:
        """Creates a row with name only; configuration defaults to {}."""
        service, client = _make_service(tmp_config_dir)

        result = service.create_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="My Row",
        )

        client.create_config_row.assert_called_once()
        call_kwargs = client.create_config_row.call_args.kwargs
        assert call_kwargs["name"] == "My Row"
        assert call_kwargs["configuration"] == {}
        assert call_kwargs["description"] == ""
        assert result["project_alias"] == "prod"

    def test_create_with_configuration(self, tmp_config_dir: Path) -> None:
        """Creates a row with explicit configuration dict."""
        service, client = _make_service(tmp_config_dir)
        cfg = {"parameters": {"table": "invoices"}}

        service.create_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="Invoices Row",
            configuration=cfg,
        )

        call_kwargs = client.create_config_row.call_args.kwargs
        assert call_kwargs["configuration"] == cfg

    def test_create_with_description(self, tmp_config_dir: Path) -> None:
        """Creates a row with optional description."""
        service, client = _make_service(tmp_config_dir)

        service.create_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="Row",
            description="Row for invoices table",
        )

        call_kwargs = client.create_config_row.call_args.kwargs
        assert call_kwargs["description"] == "Row for invoices table"

    def test_create_with_branch(self, tmp_config_dir: Path) -> None:
        """Branch ID is passed through to the client."""
        service, client = _make_service(tmp_config_dir)

        service.create_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            name="Row",
            branch_id=42,
        )

        call_kwargs = client.create_config_row.call_args.kwargs
        assert call_kwargs["branch_id"] == 42

    def test_result_contains_project_alias(self, tmp_config_dir: Path) -> None:
        """Result dict is enriched with project_alias."""
        service, _ = _make_service(tmp_config_dir)

        result = service.create_config_row(
            alias="prod",
            component_id="comp",
            config_id="cfg",
            name="Row",
        )

        assert result["project_alias"] == "prod"

    def test_api_error_propagates(self, tmp_config_dir: Path) -> None:
        """KeboolaApiError from client propagates to caller."""
        service, client = _make_service(tmp_config_dir)
        client.create_config_row.side_effect = KeboolaApiError(
            status_code=404, error_code="NOT_FOUND", message="Config not found"
        )

        with pytest.raises(KeboolaApiError, match="Config not found"):
            service.create_config_row(
                alias="prod",
                component_id="comp",
                config_id="cfg-missing",
                name="Row",
            )


# ---------------------------------------------------------------------------
# update_config_row tests
# ---------------------------------------------------------------------------


class TestUpdateConfigRow:
    """Tests for ConfigService.update_config_row."""

    def test_name_only_update(self, tmp_config_dir: Path) -> None:
        """Updating only the name does not fetch current config."""
        service, client = _make_service(tmp_config_dir)

        service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            name="New Name",
        )

        call_kwargs = client.update_config_row.call_args.kwargs
        assert call_kwargs["name"] == "New Name"
        assert call_kwargs["configuration"] is None
        client.get_config_row.assert_not_called()

    def test_full_replace(self, tmp_config_dir: Path) -> None:
        """Without --merge, configuration is sent as-is (no fetch needed)."""
        service, client = _make_service(tmp_config_dir)
        new_cfg = {"parameters": {"table": "users"}}

        service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            configuration=new_cfg,
        )

        call_kwargs = client.update_config_row.call_args.kwargs
        assert call_kwargs["configuration"] == new_cfg
        client.get_config_row.assert_not_called()

    def test_merge_preserves_siblings(self, tmp_config_dir: Path) -> None:
        """With merge=True, sibling keys are preserved."""
        service, client = _make_service(tmp_config_dir)
        partial = {"parameters": {"limit": 9999}}

        service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            configuration=partial,
            merge=True,
        )

        merged = client.update_config_row.call_args.kwargs["configuration"]
        # Original key preserved
        assert merged["parameters"]["table"] == "orders"
        # Merged key applied
        assert merged["parameters"]["limit"] == 9999

    def test_set_path_preserves_siblings(self, tmp_config_dir: Path) -> None:
        """--set targets a specific key without touching siblings."""
        service, client = _make_service(tmp_config_dir)

        service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            set_paths=[("parameters.table", "invoices")],
        )

        cfg = client.update_config_row.call_args.kwargs["configuration"]
        # Changed key
        assert cfg["parameters"]["table"] == "invoices"
        # Sibling key preserved
        assert cfg["parameters"]["limit"] == 1000

    def test_dry_run_returns_diff(self, tmp_config_dir: Path) -> None:
        """dry_run=True returns changes without calling update_config_row."""
        service, client = _make_service(tmp_config_dir)

        result = service.update_config_row(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="cfg-001",
            row_id="row-001",
            set_paths=[("parameters.table", "changed_table")],
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert len(result["changes"]) >= 1
        assert any("parameters.table" in c for c in result["changes"])
        client.update_config_row.assert_not_called()

    def test_validation_error_when_nothing_provided(self, tmp_config_dir: Path) -> None:
        """Raise error if no metadata or configuration is given."""
        service, _ = _make_service(tmp_config_dir)

        with pytest.raises(KeboolaApiError, match="must be provided"):
            service.update_config_row(
                alias="prod",
                component_id="comp",
                config_id="cfg",
                row_id="row-001",
            )

    def test_result_contains_project_alias(self, tmp_config_dir: Path) -> None:
        """Result dict is enriched with project_alias."""
        service, _ = _make_service(tmp_config_dir)

        result = service.update_config_row(
            alias="prod",
            component_id="comp",
            config_id="cfg",
            row_id="row-001",
            name="New Name",
        )

        assert result["project_alias"] == "prod"

    def test_branch_id_passed_through(self, tmp_config_dir: Path) -> None:
        """branch_id is forwarded to the client."""
        service, client = _make_service(tmp_config_dir)

        service.update_config_row(
            alias="prod",
            component_id="comp",
            config_id="cfg",
            row_id="row-001",
            name="Name",
            branch_id=99,
        )

        call_kwargs = client.update_config_row.call_args.kwargs
        assert call_kwargs["branch_id"] == 99


# ---------------------------------------------------------------------------
# get_oauth_url tests
# ---------------------------------------------------------------------------


class TestGetOauthUrl:
    """Tests for ConfigService.get_oauth_url."""

    def test_returns_url(self, tmp_config_dir: Path) -> None:
        """Returns a dict with a 'url' key containing the OAuth URL."""
        service, _client = _make_service(tmp_config_dir)

        result = service.get_oauth_url(
            alias="prod",
            component_id="keboola.ex-google-drive",
            config_id="cfg-001",
        )

        assert "url" in result
        assert result["url"].startswith("https://external.keboola.com")
        assert result["component_id"] == "keboola.ex-google-drive"
        assert result["config_id"] == "cfg-001"
        assert result["project_alias"] == "prod"

    def test_client_called_with_correct_args(self, tmp_config_dir: Path) -> None:
        """The client's get_oauth_url is invoked with component_id and config_id."""
        service, client = _make_service(tmp_config_dir)

        service.get_oauth_url(
            alias="prod",
            component_id="keboola.ex-gmail",
            config_id="gmail-cfg",
        )

        client.get_oauth_url.assert_called_once_with(
            component_id="keboola.ex-gmail",
            config_id="gmail-cfg",
        )

    def test_api_error_propagates(self, tmp_config_dir: Path) -> None:
        """KeboolaApiError from token creation propagates to caller."""
        service, client = _make_service(tmp_config_dir)
        client.get_oauth_url.side_effect = KeboolaApiError(
            status_code=403, error_code="ACCESS_DENIED", message="Token creation denied"
        )

        with pytest.raises(KeboolaApiError, match="Token creation denied"):
            service.get_oauth_url(
                alias="prod",
                component_id="keboola.ex-google-drive",
                config_id="cfg-001",
            )
