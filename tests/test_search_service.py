"""Tests for SearchService — textual and config-based search across projects."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.search_service import (
    SearchService,
    _normalise_item,
    _resolve_api_types,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TEST_TOKEN = "test-token-12345"
TEST_STACK_URL = "https://connection.keboola.com"


def _make_store(tmp_path: Path, projects: dict[str, dict] | None = None) -> ConfigStore:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", TEST_STACK_URL),
                    token=info.get("token", TEST_TOKEN),
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


def _make_verify_response(project_id: int = 1234) -> TokenVerifyResponse:
    return TokenVerifyResponse(
        token_id="1",
        token_description="test",
        project_id=project_id,
        project_name="Test Project",
        owner_name="Test Project",
        default_backend="snowflake",
        features=[],
    )


def _make_mock_client(
    project_id: int = 1234,
    global_search_result: dict | None = None,
) -> MagicMock:
    mock = MagicMock()
    mock.verify_token.return_value = _make_verify_response(project_id)
    mock.global_search.return_value = global_search_result or {"all": 0, "items": []}
    return mock


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------


class TestResolveApiTypes:
    def test_none_returns_empty(self) -> None:
        assert _resolve_api_types(None) == []

    def test_empty_returns_empty(self) -> None:
        assert _resolve_api_types([]) == []

    def test_table_maps_correctly(self) -> None:
        assert _resolve_api_types(["table"]) == ["table"]

    def test_bucket_maps_correctly(self) -> None:
        assert _resolve_api_types(["bucket"]) == ["bucket"]

    def test_config_maps_to_configuration(self) -> None:
        assert _resolve_api_types(["config"]) == ["configuration"]

    def test_flow_maps_correctly(self) -> None:
        assert _resolve_api_types(["flow"]) == ["flow"]

    def test_multiple_types_deduped(self) -> None:
        result = _resolve_api_types(["table", "bucket"])
        assert "table" in result
        assert "bucket" in result
        assert len(result) == 2

    def test_unknown_type_passed_through(self) -> None:
        # Unknown types are passed directly to the API.
        result = _resolve_api_types(["unknown-type"])
        assert result == ["unknown-type"]


class TestNormaliseItem:
    def test_table_item(self) -> None:
        raw = {
            "id": "in.c-main.orders",
            "name": "orders",
            "type": "table",
            "fullPath": {"description": "Order data"},
            "componentId": None,
            "projectId": 42,
            "projectName": "Prod",
        }
        result = _normalise_item("prod", raw)
        assert result["project_alias"] == "prod"
        assert result["type"] == "table"
        assert result["id"] == "in.c-main.orders"
        assert result["name"] == "orders"
        assert result["description"] == "Order data"
        assert result["project_id"] == 42

    def test_config_item(self) -> None:
        raw = {
            "id": "123",
            "name": "My Extractor",
            "type": "configuration",
            "fullPath": {},
            "componentId": "keboola.ex-db-snowflake",
            "projectId": 1,
            "projectName": "Dev",
        }
        result = _normalise_item("dev", raw)
        assert result["component_id"] == "keboola.ex-db-snowflake"
        assert result["type"] == "configuration"


# ---------------------------------------------------------------------------
# SearchService.search() — textual mode
# ---------------------------------------------------------------------------


class TestSearchServiceTextual:
    def test_single_project_success(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        mock_client = _make_mock_client(
            global_search_result={
                "all": 1,
                "items": [
                    {
                        "id": "in.c-main.orders",
                        "name": "orders",
                        "type": "table",
                        "fullPath": {},
                        "componentId": None,
                        "projectId": 1234,
                        "projectName": "Prod",
                    }
                ],
            }
        )
        service = SearchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        result = service.search(query="orders")

        assert result["stats"]["projects_searched"] == 1
        assert result["stats"]["results_found"] == 1
        assert result["errors"] == []
        assert len(result["results"]) == 1
        assert result["results"][0]["project_alias"] == "prod"
        assert result["results"][0]["type"] == "table"
        assert result["results"][0]["id"] == "in.c-main.orders"

    def test_no_results(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        mock_client = _make_mock_client(global_search_result={"all": 0, "items": []})
        service = SearchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        result = service.search(query="xyzzy_nonexistent")

        assert result["stats"]["results_found"] == 0
        assert result["results"] == []
        assert result["errors"] == []

    def test_multi_project_fan_out(self, tmp_path: Path) -> None:
        store = _make_store(
            tmp_path,
            {
                "prod": {"token": TEST_TOKEN, "project_id": 1},
                "dev": {"token": "other-token", "project_id": 2},
            },
        )

        def factory(url: str, token: str) -> MagicMock:
            pid = 1 if token == TEST_TOKEN else 2
            return _make_mock_client(
                project_id=pid,
                global_search_result={
                    "all": 1,
                    "items": [
                        {
                            "id": f"bucket-{pid}",
                            "name": "sales",
                            "type": "bucket",
                            "fullPath": {},
                            "componentId": None,
                            "projectId": pid,
                            "projectName": f"Project {pid}",
                        }
                    ],
                },
            )

        service = SearchService(config_store=store, client_factory=factory)
        result = service.search(query="sales")

        assert result["stats"]["projects_searched"] == 2
        assert result["stats"]["results_found"] == 2
        assert result["errors"] == []
        project_aliases = {r["project_alias"] for r in result["results"]}
        assert project_aliases == {"prod", "dev"}

    def test_project_api_error_accumulates(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import KeboolaApiError

        store = _make_store(
            tmp_path,
            {
                "prod": {"token": TEST_TOKEN},
                "broken": {"token": "bad-token"},
            },
        )

        def factory(url: str, token: str) -> MagicMock:
            mock = MagicMock()
            if token == "bad-token":
                mock.verify_token.side_effect = KeboolaApiError(
                    "INVALID_TOKEN", "Token is invalid."
                )
            else:
                mock.verify_token.return_value = _make_verify_response()
                mock.global_search.return_value = {"all": 0, "items": []}
            return mock

        service = SearchService(config_store=store, client_factory=factory)
        result = service.search(query="test")

        assert result["stats"]["projects_searched"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "broken"

    def test_type_filter_passed_to_client(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        mock_client = _make_mock_client()
        service = SearchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        service.search(query="test", item_types=["table", "bucket"])

        # Verify global_search was called with the correct types.
        call_kwargs = mock_client.global_search.call_args
        types_arg = (
            call_kwargs.kwargs.get("types") or call_kwargs[1].get("types") or call_kwargs[0][2]
        )
        assert "table" in types_arg
        assert "bucket" in types_arg

    def test_specific_project_alias(self, tmp_path: Path) -> None:
        store = _make_store(
            tmp_path,
            {
                "prod": {"token": TEST_TOKEN},
                "dev": {"token": "other-token"},
            },
        )
        mock_client = _make_mock_client()
        service = SearchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )
        result = service.search(query="test", aliases=["prod"])

        # Only one project searched.
        assert result["stats"]["projects_searched"] == 1

    def test_unknown_project_alias_raises_config_error(self, tmp_path: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        service = SearchService(config_store=store)

        with pytest.raises(ConfigError):
            service.search(query="test", aliases=["nonexistent"])


# ---------------------------------------------------------------------------
# SearchService.search() — config-based mode
# ---------------------------------------------------------------------------


class TestSearchServiceConfigBased:
    def test_config_based_delegates_to_config_service(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        service = SearchService(config_store=store)

        mock_result = {
            "matches": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "123",
                    "config_name": "My Extractor",
                    "description": "",
                    "match_count": 2,
                    "match_locations": ["parameters.db.host"],
                }
            ],
            "errors": [],
            "stats": {
                "projects_searched": 1,
                "configs_searched": 5,
                "matches_found": 1,
            },
        }

        with patch("keboola_agent_cli.services.search_service.ConfigService") as MockConfigService:
            mock_cs = MagicMock()
            mock_cs.search_configs.return_value = mock_result
            MockConfigService.return_value = mock_cs

            result = service.search(query="snowflake", search_type="config-based")

        assert result["stats"]["results_found"] == 1
        assert len(result["results"]) == 1
        assert result["results"][0]["type"] == "configuration"
        assert result["results"][0]["id"] == "123"
        assert result["results"][0]["name"] == "My Extractor"
        assert result["results"][0]["component_id"] == "keboola.ex-db-snowflake"

    def test_config_based_empty_result(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path, {"prod": {"token": TEST_TOKEN}})
        service = SearchService(config_store=store)

        mock_result = {
            "matches": [],
            "errors": [],
            "stats": {"projects_searched": 1, "configs_searched": 3, "matches_found": 0},
        }

        with patch("keboola_agent_cli.services.search_service.ConfigService") as MockConfigService:
            mock_cs = MagicMock()
            mock_cs.search_configs.return_value = mock_result
            MockConfigService.return_value = mock_cs

            result = service.search(query="xyzzy", search_type="config-based")

        assert result["results"] == []
        assert result["stats"]["results_found"] == 0
