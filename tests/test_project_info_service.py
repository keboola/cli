"""Unit tests for ProjectService.get_info()."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.project_service import ProjectService

# Minimal raw API response from /v2/storage/tokens/verify
RAW_API_RESPONSE = {
    "id": 99,
    "description": "Agent token",
    "isMasterToken": False,
    "expires": None,
    "owner": {
        "id": 1234,
        "name": "Production",
        "defaultBackend": "snowflake",
        "features": ["storage-branches", "orchestrator-tasks"],
        "limits": {
            "dataSizeBytes": {"name": "dataSizeBytes", "value": 5000000000},
        },
        "metrics": {
            "dataSizeBytes": 123456,
        },
    },
}


def _make_service(tmp_config_dir: Path, raw_response: dict) -> ProjectService:
    """Create ProjectService with one project and a client returning raw_response."""
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-xxx",
            project_name="Production",
            project_id=1234,
        ),
    )

    mock_client = MagicMock()
    mock_client.get_project_info.return_value = raw_response

    service = ProjectService(
        config_store=store,
        client_factory=lambda url, token: mock_client,
    )
    return service, mock_client


class TestGetInfo:
    """Tests for ProjectService.get_info()."""

    def test_get_info_success_basic_fields(self, tmp_config_dir: Path) -> None:
        """get_info returns structured dict with all expected fields."""
        service, _mock_client = _make_service(tmp_config_dir, RAW_API_RESPONSE)
        result = service.get_info(alias="prod")

        assert result["alias"] == "prod"
        assert result["project_id"] == 1234
        assert result["project_name"] == "Production"
        assert result["stack_url"] == "https://connection.keboola.com"
        assert result["default_backend"] == "snowflake"

    def test_get_info_token_fields(self, tmp_config_dir: Path) -> None:
        """get_info extracts token metadata correctly."""
        service, _ = _make_service(tmp_config_dir, RAW_API_RESPONSE)
        result = service.get_info(alias="prod")

        assert result["token_id"] == "99"
        assert result["token_description"] == "Agent token"
        assert result["is_master_token"] is False
        assert result["token_expires"] is None

    def test_get_info_features_list(self, tmp_config_dir: Path) -> None:
        """get_info includes the full features list from owner.features."""
        service, _ = _make_service(tmp_config_dir, RAW_API_RESPONSE)
        result = service.get_info(alias="prod")

        assert "storage-branches" in result["features"]
        assert "orchestrator-tasks" in result["features"]

    def test_get_info_limits_and_metrics(self, tmp_config_dir: Path) -> None:
        """get_info includes limits and metrics dicts."""
        service, _ = _make_service(tmp_config_dir, RAW_API_RESPONSE)
        result = service.get_info(alias="prod")

        assert "dataSizeBytes" in result["limits"]
        assert result["metrics"]["dataSizeBytes"] == 123456

    def test_get_info_calls_client_and_closes(self, tmp_config_dir: Path) -> None:
        """get_info calls get_project_info() and always closes the client."""
        service, mock_client = _make_service(tmp_config_dir, RAW_API_RESPONSE)
        service.get_info(alias="prod")

        mock_client.get_project_info.assert_called_once()
        mock_client.close.assert_called_once()

    def test_get_info_missing_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """get_info raises ConfigError for an unknown alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: MagicMock(),
        )

        with pytest.raises(ConfigError, match="not found"):
            service.get_info(alias="nonexistent")

    def test_get_info_api_error_propagates(self, tmp_config_dir: Path) -> None:
        """get_info propagates KeboolaApiError from the client."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx",
                project_name="Production",
                project_id=1234,
            ),
        )

        mock_client = MagicMock()
        mock_client.get_project_info.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_info(alias="prod")

        assert exc_info.value.error_code == "INVALID_TOKEN"
        # Client must still be closed even on error
        mock_client.close.assert_called_once()

    def test_get_info_empty_owner_fields(self, tmp_config_dir: Path) -> None:
        """get_info handles missing/empty owner fields gracefully."""
        sparse_response = {
            "id": 42,
            "description": "",
            "isMasterToken": True,
            "expires": "2030-01-01T00:00:00+00:00",
            "owner": {},
        }
        service, _ = _make_service(tmp_config_dir, sparse_response)
        result = service.get_info(alias="prod")

        assert result["project_id"] is None
        assert result["project_name"] == ""
        assert result["default_backend"] == "snowflake"
        assert result["features"] == []
        assert result["limits"] == {}
        assert result["metrics"] == {}
        assert result["is_master_token"] is True
        assert result["token_expires"] == "2030-01-01T00:00:00+00:00"

    def test_get_info_bigquery_backend(self, tmp_config_dir: Path) -> None:
        """get_info correctly reads defaultBackend = bigquery."""
        response = dict(RAW_API_RESPONSE)
        response["owner"] = dict(RAW_API_RESPONSE["owner"])
        response["owner"]["defaultBackend"] = "bigquery"

        service, _ = _make_service(tmp_config_dir, response)
        result = service.get_info(alias="prod")

        assert result["default_backend"] == "bigquery"
