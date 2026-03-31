"""Tests for ConfigService.search_configs() and _find_matches_in_json helper."""

from pathlib import Path
from unittest.mock import MagicMock

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.services.config_service import ConfigService, _find_matches_in_json

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_list_components_client(components: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_components returning given data."""
    mock_client = MagicMock()
    mock_client.list_components.return_value = components
    return mock_client


# ---------------------------------------------------------------------------
# Sample data that resembles real Keboola API responses
# ---------------------------------------------------------------------------

SAMPLE_COMPONENTS_RICH = [
    {
        "id": "keboola.ex-db-snowflake",
        "name": "Snowflake Extractor",
        "type": "extractor",
        "configurations": [
            {
                "id": "123",
                "name": "My Extractor",
                "description": "Extracts marketing data",
                "configuration": {
                    "parameters": {
                        "db": {
                            "host": "account.snowflakecomputing.com",
                            "#password": "heslo123",
                            "port": 443,
                        },
                        "tables": [
                            {"tableName": "campaigns", "schema": "PUBLIC"},
                            {"tableName": "ad_groups", "schema": "PUBLIC"},
                        ],
                    }
                },
                "rows": [],
            },
            {
                "id": "456",
                "name": "Staging Extractor",
                "description": "Staging env loader",
                "configuration": {
                    "parameters": {
                        "db": {
                            "host": "staging.snowflakecomputing.com",
                            "#password": "staging-pass",
                        }
                    }
                },
                "rows": [],
            },
        ],
    },
    {
        "id": "keboola.wr-db-snowflake",
        "name": "Snowflake Writer",
        "type": "writer",
        "configurations": [
            {
                "id": "789",
                "name": "Write to DWH",
                "description": "Writes aggregated data to warehouse",
                "configuration": {
                    "parameters": {
                        "db": {
                            "host": "dwh.snowflakecomputing.com",
                            "#password": "dwh-secret",
                        }
                    }
                },
                "rows": [],
            },
        ],
    },
]

SAMPLE_COMPONENTS_DEV = [
    {
        "id": "keboola.python-transformation-v2",
        "name": "Python Transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "301",
                "name": "Aggregate 100-200 Data",
                "description": "Aggregation script with phone 555-1234",
                "configuration": {
                    "parameters": {
                        "script": "import pandas as pd\ndf = df.groupby('campaign').sum()",
                    }
                },
                "rows": [],
            },
        ],
    },
]


# ===========================================================================
# Tests for _find_matches_in_json
# ===========================================================================


class TestFindMatchesInJson:
    """Tests for the _find_matches_in_json recursive helper."""

    def test_find_match_in_string_value(self) -> None:
        """Simple string match returns the correct path."""
        obj = {"name": "My Extractor", "type": "extractor"}
        match_fn = lambda s: "Extractor" in s  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert "name" in paths
        assert len(paths) == 1

    def test_find_match_in_nested_dict(self) -> None:
        """Match in a deeply nested dict returns dotted path."""
        obj = {
            "configuration": {
                "parameters": {
                    "db": {
                        "host": "account.snowflakecomputing.com",
                    }
                }
            }
        }
        match_fn = lambda s: "snowflakecomputing" in s  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert paths == ["configuration.parameters.db.host"]

    def test_find_match_in_list(self) -> None:
        """Match in array items includes [index] in path."""
        obj = {
            "tables": [
                {"tableName": "campaigns"},
                {"tableName": "ad_groups"},
            ]
        }
        match_fn = lambda s: "ad_groups" in s  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert paths == ["tables[1].tableName"]

    def test_find_match_in_number(self) -> None:
        """Numeric values are converted to string for matching."""
        obj = {"port": 443, "name": "test"}
        match_fn = lambda s: "443" in s  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert "port" in paths

    def test_no_match_returns_empty(self) -> None:
        """When nothing matches, an empty list is returned."""
        obj = {
            "name": "Production",
            "configuration": {"parameters": {"key": "value"}},
        }
        match_fn = lambda s: "nonexistent_string_xyz" in s  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert paths == []

    def test_find_multiple_matches(self) -> None:
        """Same substring in multiple locations returns all paths."""
        obj = {
            "name": "snowflake loader",
            "description": "Loads from snowflake",
            "configuration": {
                "parameters": {
                    "db": {
                        "host": "account.snowflakecomputing.com",
                    }
                }
            },
        }
        match_fn = lambda s: "snowflake" in s.lower()  # noqa: E731

        paths = _find_matches_in_json(obj, match_fn)

        assert len(paths) == 3
        assert "name" in paths
        assert "description" in paths
        assert "configuration.parameters.db.host" in paths


# ===========================================================================
# Tests for ConfigService.search_configs
# ===========================================================================


class TestSearchConfigs:
    """Tests for ConfigService.search_configs()."""

    def test_search_substring_case_sensitive(self, tmp_config_dir: Path) -> None:
        """Default search is case-sensitive substring match."""
        store = setup_single_project(tmp_config_dir)
        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_RICH)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(query="account.snowflakecomputing.com")

        assert len(result["errors"]) == 0
        matches = result["matches"]
        # Only config 123 has "account.snowflakecomputing.com"
        assert len(matches) == 1
        assert matches[0]["config_id"] == "123"
        assert matches[0]["component_id"] == "keboola.ex-db-snowflake"
        assert "configuration.parameters.db.host" in matches[0]["match_locations"]
        assert matches[0]["match_count"] >= 1

        # Case-sensitive: uppercase should NOT match
        result_upper = service.search_configs(query="Account.Snowflakecomputing.com")
        assert len(result_upper["matches"]) == 0

    def test_search_no_match(self, tmp_config_dir: Path) -> None:
        """When nothing matches, returns empty matches with correct stats."""
        store = setup_single_project(tmp_config_dir)
        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_RICH)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(query="this_will_never_match_anything_xyz")

        assert result["matches"] == []
        assert result["errors"] == []
        assert result["stats"]["projects_searched"] == 1
        # 2 extractor configs + 1 writer config = 3 total
        assert result["stats"]["configs_searched"] == 3
        assert result["stats"]["matches_found"] == 0

    def test_search_ignore_case(self, tmp_config_dir: Path) -> None:
        """Case-insensitive search finds matches regardless of casing."""
        store = setup_single_project(tmp_config_dir)
        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_RICH)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(query="SNOWFLAKECOMPUTING", ignore_case=True)

        matches = result["matches"]
        # All 3 configs have "snowflakecomputing" in their host
        assert len(matches) == 3
        config_ids = {m["config_id"] for m in matches}
        assert config_ids == {"123", "456", "789"}

    def test_search_regex(self, tmp_config_dir: Path) -> None:
        r"""Regex pattern like \d{3}-\d+ matches in config bodies."""
        store = setup_single_project(tmp_config_dir, alias="dev")
        mock_client = _make_list_components_client(SAMPLE_COMPONENTS_DEV)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(query=r"\d{3}-\d+", use_regex=True)

        matches = result["matches"]
        assert len(matches) == 1
        assert matches[0]["config_id"] == "301"
        # The match is in the description field ("555-1234")
        assert any("description" in loc for loc in matches[0]["match_locations"])

    def test_search_multi_project(self, tmp_config_dir: Path) -> None:
        """Search across two projects aggregates results from both."""
        store = setup_two_projects(tmp_config_dir)

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS_RICH)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_DEV)

        def factory(url: str, token: str) -> MagicMock:
            if token == "901-xxx":
                return prod_client
            return dev_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        # "data" appears in descriptions of both projects
        result = service.search_configs(query="data", ignore_case=True)

        assert len(result["errors"]) == 0
        assert result["stats"]["projects_searched"] == 2

        # Verify matches come from both projects
        project_aliases = {m["project_alias"] for m in result["matches"]}
        assert "prod" in project_aliases
        assert "dev" in project_aliases

        # Stats should reflect total configs from both projects
        # prod: 3 configs (2 extractor + 1 writer), dev: 1 config
        assert result["stats"]["configs_searched"] == 4

    def test_search_with_component_type_filter(self, tmp_config_dir: Path) -> None:
        """Filtering by component_type narrows down the search scope."""
        store = setup_single_project(tmp_config_dir)

        # Return only extractors when component_type filter is applied
        extractor_only = [c for c in SAMPLE_COMPONENTS_RICH if c["type"] == "extractor"]
        mock_client = _make_list_components_client(extractor_only)

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(
            query="snowflakecomputing",
            ignore_case=True,
            component_type="extractor",
        )

        matches = result["matches"]
        # Only extractor configs (123 and 456), not the writer (789)
        assert len(matches) == 2
        for m in matches:
            assert m["component_type"] == "extractor"

        # The client was called with the component_type filter
        mock_client.list_components.assert_called_once_with(
            component_type="extractor", branch_id=None
        )
