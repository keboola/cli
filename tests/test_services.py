"""Tests for ProjectService and ConfigService."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.project_service import ProjectService


def _make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
    token_description: str = "My Token",
) -> MagicMock:
    """Create a mock KeboolaClient that returns a successful verify_token response."""
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description=token_description,
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return mock_client


def _make_failing_client(error: KeboolaApiError) -> MagicMock:
    """Create a mock KeboolaClient whose verify_token raises the given error."""
    mock_client = MagicMock()
    mock_client.verify_token.side_effect = error
    return mock_client


class TestAddProject:
    """Tests for ProjectService.add_project()."""

    def test_add_project_success(self, tmp_config_dir: Path) -> None:
        """add_project verifies token, saves to config, returns project info."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client(project_name="Production", project_id=9999)

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.add_project(
            alias="prod",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        assert result["alias"] == "prod"
        assert result["project_name"] == "Production"
        assert result["project_id"] == 9999
        assert result["stack_url"] == "https://connection.keboola.com"
        assert "901-...pt0k" in result["token"]

        # Verify it's persisted
        project = store.get_project("prod")
        assert project is not None
        assert project.project_name == "Production"

        mock_client.verify_token.assert_called_once()
        mock_client.close.assert_called_once()

    def test_add_project_invalid_token(self, tmp_config_dir: Path) -> None:
        """add_project raises KeboolaApiError when token verification fails."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_failing_client(
            KeboolaApiError(
                message="Invalid token",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
        )

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            service.add_project(
                alias="bad",
                stack_url="https://connection.keboola.com",
                token="invalid-token-abcdefgh",
            )

        assert exc_info.value.error_code == "INVALID_TOKEN"

        # Project should NOT be saved on failure
        assert store.get_project("bad") is None

    def test_add_project_duplicate_alias(self, tmp_config_dir: Path) -> None:
        """add_project raises ConfigError when alias already exists."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="test",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        with pytest.raises(ConfigError, match="already exists"):
            service.add_project(
                alias="test",
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            )

    def test_add_project_network_error(self, tmp_config_dir: Path) -> None:
        """add_project raises KeboolaApiError on network timeout."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_failing_client(
            KeboolaApiError(
                message="Request timed out",
                status_code=0,
                error_code="TIMEOUT",
                retryable=True,
            )
        )

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            service.add_project(
                alias="timeout",
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            )

        assert exc_info.value.error_code == "TIMEOUT"
        assert exc_info.value.retryable is True


class TestRemoveProject:
    """Tests for ProjectService.remove_project()."""

    def test_remove_project_success(self, tmp_config_dir: Path) -> None:
        """remove_project removes the project and returns confirmation."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="test",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        result = service.remove_project("test")
        assert result["alias"] == "test"
        assert "removed" in result["message"].lower()
        assert store.get_project("test") is None

    def test_remove_nonexistent_raises_error(self, tmp_config_dir: Path) -> None:
        """remove_project raises ConfigError for nonexistent alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.remove_project("nonexistent")


class TestEditProject:
    """Tests for ProjectService.edit_project()."""

    def test_edit_url_only(self, tmp_config_dir: Path) -> None:
        """edit_project with only URL updates the stack URL without re-verifying."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="test",
            stack_url="https://old.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        # Reset mock to track new calls
        mock_client.verify_token.reset_mock()

        result = service.edit_project("test", stack_url="https://new.com")
        assert result["stack_url"] == "https://new.com"

        # verify_token should NOT be called when only URL changes
        mock_client.verify_token.assert_not_called()

    def test_edit_token_reverifies(self, tmp_config_dir: Path) -> None:
        """edit_project with new token re-verifies against the API."""
        store = ConfigStore(config_dir=tmp_config_dir)
        initial_client = _make_mock_client(project_name="Old Project", project_id=1000)
        new_client = _make_mock_client(project_name="New Project", project_id=2000)

        call_count = [0]

        def factory(url, token):
            call_count[0] += 1
            if call_count[0] <= 1:
                return initial_client
            return new_client

        service = ProjectService(
            config_store=store,
            client_factory=factory,
        )

        service.add_project(
            alias="test",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        result = service.edit_project(
            "test",
            token="902-newtoken-ABCDEFGHIJKLMNOP",
        )

        assert result["project_name"] == "New Project"
        assert result["project_id"] == 2000

    def test_edit_no_changes_raises_error(self, tmp_config_dir: Path) -> None:
        """edit_project with no changes raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="test",
            stack_url="https://a.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        with pytest.raises(ConfigError, match="No changes"):
            service.edit_project("test")

    def test_edit_nonexistent_raises_error(self, tmp_config_dir: Path) -> None:
        """edit_project for nonexistent alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.edit_project("nonexistent", stack_url="https://new.com")


class TestListProjects:
    """Tests for ProjectService.list_projects()."""

    def test_list_empty(self, tmp_config_dir: Path) -> None:
        """list_projects with no projects returns empty list."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.list_projects()
        assert result == []

    def test_list_multiple_projects(self, tmp_config_dir: Path) -> None:
        """list_projects returns all projects with masked tokens."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="prod",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )
        service.add_project(
            alias="dev",
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="532-abcdef-ghijklmnop",
        )

        result = service.list_projects()
        assert len(result) == 2

        aliases = {p["alias"] for p in result}
        assert aliases == {"prod", "dev"}

        # Tokens must be masked
        for p in result:
            assert "10493007" not in p["token"]
            assert "abcdef" not in p["token"]

        # First project should be default
        prod = next(p for p in result if p["alias"] == "prod")
        assert prod["is_default"] is True

    def test_list_projects_token_never_fully_shown(self, tmp_config_dir: Path) -> None:
        """list_projects never returns the full token."""
        store = ConfigStore(config_dir=tmp_config_dir)
        full_token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="test",
            stack_url="https://connection.keboola.com",
            token=full_token,
        )

        result = service.list_projects()
        assert result[0]["token"] != full_token
        assert "901-...pt0k" == result[0]["token"]


class TestGetStatus:
    """Tests for ProjectService.get_status()."""

    def test_status_all_ok(self, tmp_config_dir: Path) -> None:
        """get_status returns OK status with response time for healthy projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client(project_name="Production", project_id=1234)
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.add_project(
            alias="prod",
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        )

        result = service.get_status()
        assert len(result) == 1
        assert result[0]["alias"] == "prod"
        assert result[0]["status"] == "ok"
        assert "response_time_ms" in result[0]
        assert result[0]["project_name"] == "Production"
        assert isinstance(result[0]["response_time_ms"], int)

    def test_status_mixed_success_failure(self, tmp_config_dir: Path) -> None:
        """get_status handles mixed success/failure across projects."""
        store = ConfigStore(config_dir=tmp_config_dir)

        ok_client = _make_mock_client(project_name="OK Project")
        fail_client = _make_failing_client(
            KeboolaApiError(
                message="Token expired",
                status_code=401,
                error_code="INVALID_TOKEN",
            )
        )

        call_count = [0]

        def factory(url, token):
            call_count[0] += 1
            if "ok" in token:
                return ok_client
            return fail_client

        service = ProjectService(
            config_store=store,
            client_factory=factory,
        )

        store.add_project("ok-project", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-ok-abcdefghijklmnop",
            project_name="OK",
            project_id=1,
        ))
        store.add_project("bad-project", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="902-bad-abcdefghijklmnop",
            project_name="Bad",
            project_id=2,
        ))

        result = service.get_status()
        assert len(result) == 2

        ok_entry = next(r for r in result if r["alias"] == "ok-project")
        bad_entry = next(r for r in result if r["alias"] == "bad-project")

        assert ok_entry["status"] == "ok"
        assert ok_entry["project_name"] == "OK Project"

        assert bad_entry["status"] == "error"
        assert bad_entry["error_code"] == "INVALID_TOKEN"
        assert "Token expired" in bad_entry["error"]

    def test_status_specific_project(self, tmp_config_dir: Path) -> None:
        """get_status with specific alias only checks that project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        store.add_project("first", ProjectConfig(
            stack_url="https://a.com",
            token="901-abcdef-12345678",
        ))
        store.add_project("second", ProjectConfig(
            stack_url="https://b.com",
            token="902-abcdef-12345678",
        ))

        result = service.get_status(aliases=["first"])
        assert len(result) == 1
        assert result[0]["alias"] == "first"

    def test_status_nonexistent_alias_raises_error(self, tmp_config_dir: Path) -> None:
        """get_status with nonexistent alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.get_status(aliases=["nonexistent"])

    def test_status_token_masked(self, tmp_config_dir: Path) -> None:
        """get_status always masks tokens in output."""
        store = ConfigStore(config_dir=tmp_config_dir)
        full_token = "901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k"
        mock_client = _make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        store.add_project("test", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token=full_token,
        ))

        result = service.get_status()
        assert result[0]["token"] != full_token
        assert "901-...pt0k" == result[0]["token"]


# ---------------------------------------------------------------------------
# Helpers for ConfigService tests
# ---------------------------------------------------------------------------

def _make_list_components_client(
    components: list[dict],
) -> MagicMock:
    """Create a mock KeboolaClient with list_components returning given data."""
    mock_client = MagicMock()
    mock_client.list_components.return_value = components
    return mock_client


SAMPLE_COMPONENTS = [
    {
        "id": "keboola.ex-db-snowflake",
        "name": "Snowflake Extractor",
        "type": "extractor",
        "configurations": [
            {
                "id": "101",
                "name": "Production Load",
                "description": "Loads production data",
            },
            {
                "id": "102",
                "name": "Dev Load",
                "description": "Loads dev data",
            },
        ],
    },
    {
        "id": "keboola.wr-db-snowflake",
        "name": "Snowflake Writer",
        "type": "writer",
        "configurations": [
            {
                "id": "201",
                "name": "Write to DWH",
                "description": "Writes to data warehouse",
            },
        ],
    },
]

SAMPLE_COMPONENTS_2 = [
    {
        "id": "keboola.python-transformation-v2",
        "name": "Python Transformation",
        "type": "transformation",
        "configurations": [
            {
                "id": "301",
                "name": "Aggregate Data",
                "description": "Aggregation script",
            },
        ],
    },
]


class TestConfigServiceListConfigs:
    """Tests for ConfigService.list_configs()."""

    def test_list_configs_single_project_all_configs(self, tmp_config_dir: Path) -> None:
        """list_configs returns all configs from a single project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            project_name="Production",
            project_id=1234,
        ))

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        configs = result["configs"]
        errors = result["errors"]

        assert len(errors) == 0
        assert len(configs) == 3  # 2 from extractor + 1 from writer

        # Verify structure of first config
        first = configs[0]
        assert first["project_alias"] == "prod"
        assert first["component_id"] == "keboola.ex-db-snowflake"
        assert first["component_name"] == "Snowflake Extractor"
        assert first["component_type"] == "extractor"
        assert first["config_id"] == "101"
        assert first["config_name"] == "Production Load"
        assert first["config_description"] == "Loads production data"

    def test_list_configs_multi_project_aggregation(self, tmp_config_dir: Path) -> None:
        """list_configs aggregates configs across multiple projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            project_name="Production",
            project_id=1234,
        ))
        store.add_project("dev", ProjectConfig(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="532-abcdef-ghijklmnopqrst",
            project_name="Development",
            project_id=5678,
        ))

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_configs()
        configs = result["configs"]
        errors = result["errors"]

        assert len(errors) == 0
        assert len(configs) == 4  # 3 from prod + 1 from dev

        prod_configs = [c for c in configs if c["project_alias"] == "prod"]
        dev_configs = [c for c in configs if c["project_alias"] == "dev"]

        assert len(prod_configs) == 3
        assert len(dev_configs) == 1
        assert dev_configs[0]["component_id"] == "keboola.python-transformation-v2"

    def test_list_configs_filter_by_component_type(self, tmp_config_dir: Path) -> None:
        """list_configs passes component_type filter to the client."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        # When filtering by type, API returns only matching components
        extractor_only = [SAMPLE_COMPONENTS[0]]
        mock_client = _make_list_components_client(extractor_only)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs(component_type="extractor")
        configs = result["configs"]

        assert len(configs) == 2
        assert all(c["component_type"] == "extractor" for c in configs)

        # Verify the type filter was passed to the client
        mock_client.list_components.assert_called_once_with(component_type="extractor")

    def test_list_configs_filter_by_component_id(self, tmp_config_dir: Path) -> None:
        """list_configs filters configs to only the specified component_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs(component_id="keboola.wr-db-snowflake")
        configs = result["configs"]

        assert len(configs) == 1
        assert configs[0]["component_id"] == "keboola.wr-db-snowflake"
        assert configs[0]["config_name"] == "Write to DWH"

    def test_list_configs_filter_by_project_alias(self, tmp_config_dir: Path) -> None:
        """list_configs with aliases only queries specified projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))
        store.add_project("dev", ProjectConfig(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="532-abcdef-ghijklmnopqrst",
        ))

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        # Only request from prod
        result = service.list_configs(aliases=["prod"])
        configs = result["configs"]

        assert len(configs) == 3
        assert all(c["project_alias"] == "prod" for c in configs)

        # dev_client.list_components should NOT have been called
        dev_client.list_components.assert_not_called()

    def test_list_configs_partial_failure(self, tmp_config_dir: Path) -> None:
        """list_configs continues when one project fails, reporting the error."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("good", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-good-abcdefghijklmnop",
        ))
        store.add_project("bad", ProjectConfig(
            stack_url="https://connection.north-europe.azure.keboola.com",
            token="532-bad-abcdefghijklmnopq",
        ))

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        bad_client = MagicMock()
        bad_client.list_components.side_effect = KeboolaApiError(
            message="Token expired for bad project",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_configs()
        configs = result["configs"]
        errors = result["errors"]

        # Good project configs should still be present
        assert len(configs) == 3
        assert all(c["project_alias"] == "good" for c in configs)

        # Bad project error should be reported
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "bad"
        assert errors[0]["error_code"] == "INVALID_TOKEN"
        assert "Token expired" in errors[0]["message"]

    def test_list_configs_empty_results(self, tmp_config_dir: Path) -> None:
        """list_configs returns empty configs list when no configurations exist."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("empty", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        # No components returned
        mock_client = _make_list_components_client([])
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        assert result["configs"] == []
        assert result["errors"] == []

    def test_list_configs_no_projects_configured(self, tmp_config_dir: Path) -> None:
        """list_configs with no projects returns empty results."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ConfigService(config_store=store)

        result = service.list_configs()
        assert result["configs"] == []
        assert result["errors"] == []

    def test_list_configs_unknown_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """list_configs with unknown alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ConfigService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.list_configs(aliases=["nonexistent"])

    def test_list_configs_client_closed_after_use(self, tmp_config_dir: Path) -> None:
        """list_configs always closes the client after querying."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_configs()
        mock_client.close.assert_called_once()

    def test_list_configs_client_closed_on_error(self, tmp_config_dir: Path) -> None:
        """list_configs closes the client even when the API call fails."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("bad", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = MagicMock()
        mock_client.list_components.side_effect = KeboolaApiError(
            message="Server error",
            status_code=500,
            error_code="API_ERROR",
            retryable=True,
        )
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_configs()
        mock_client.close.assert_called_once()

    def test_list_configs_combined_type_and_component_id_filter(self, tmp_config_dir: Path) -> None:
        """list_configs applies both component_type and component_id filters."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs(
            component_type="extractor",
            component_id="keboola.ex-db-snowflake",
        )
        configs = result["configs"]

        assert len(configs) == 2
        assert all(c["component_id"] == "keboola.ex-db-snowflake" for c in configs)

        # component_type was passed to client
        mock_client.list_components.assert_called_once_with(component_type="extractor")

    def test_list_configs_multiple_aliases(self, tmp_config_dir: Path) -> None:
        """list_configs with multiple aliases queries exactly those projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("proj-a", ProjectConfig(
            stack_url="https://a.com",
            token="901-aaa-abcdefghijklmnop",
        ))
        store.add_project("proj-b", ProjectConfig(
            stack_url="https://b.com",
            token="902-bbb-abcdefghijklmnop",
        ))
        store.add_project("proj-c", ProjectConfig(
            stack_url="https://c.com",
            token="903-ccc-abcdefghijklmnop",
        ))

        client_a = _make_list_components_client(SAMPLE_COMPONENTS)
        client_b = _make_list_components_client(SAMPLE_COMPONENTS_2)
        client_c = _make_list_components_client([])

        def factory(url, token):
            if "aaa" in token:
                return client_a
            elif "bbb" in token:
                return client_b
            return client_c

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_configs(aliases=["proj-a", "proj-b"])
        configs = result["configs"]

        assert len(configs) == 4  # 3 from a + 1 from b
        aliases_in_result = {c["project_alias"] for c in configs}
        assert aliases_in_result == {"proj-a", "proj-b"}

        # proj-c should not have been queried
        client_c.list_components.assert_not_called()


class TestConfigServiceGetConfigDetail:
    """Tests for ConfigService.get_config_detail()."""

    def test_get_config_detail_success(self, tmp_config_dir: Path) -> None:
        """get_config_detail returns full config detail with project_alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        detail_response = {
            "id": "101",
            "name": "Production Load",
            "description": "Loads production data",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [],
        }

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="101",
        )

        assert result["id"] == "101"
        assert result["name"] == "Production Load"
        assert result["project_alias"] == "prod"
        assert result["configuration"] == {"parameters": {"db": "prod"}}
        mock_client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "101"
        )
        mock_client.close.assert_called_once()

    def test_get_config_detail_unknown_alias(self, tmp_config_dir: Path) -> None:
        """get_config_detail raises ConfigError for unknown alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ConfigService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.get_config_detail(
                alias="nonexistent",
                component_id="keboola.ex-db-snowflake",
                config_id="101",
            )

    def test_get_config_detail_api_error(self, tmp_config_dir: Path) -> None:
        """get_config_detail propagates KeboolaApiError from the client."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Config not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_config_detail(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id="999",
            )

        assert exc_info.value.error_code == "NOT_FOUND"
        mock_client.close.assert_called_once()

    def test_get_config_detail_client_closed_on_error(self, tmp_config_dir: Path) -> None:
        """get_config_detail closes the client even when API call fails."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Server error",
            status_code=500,
            error_code="API_ERROR",
            retryable=True,
        )

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError):
            service.get_config_detail("prod", "comp-x", "cfg-y")

        mock_client.close.assert_called_once()


class TestResolveProjects:
    """Tests for ConfigService.resolve_projects()."""

    def test_resolve_all_projects(self, tmp_config_dir: Path) -> None:
        """resolve_projects with no aliases returns all projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://a.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))
        store.add_project("dev", ProjectConfig(
            stack_url="https://b.com",
            token="532-abcdef-ghijklmnopqrst",
        ))

        service = ConfigService(config_store=store)
        result = service.resolve_projects()
        assert set(result.keys()) == {"prod", "dev"}

    def test_resolve_specific_aliases(self, tmp_config_dir: Path) -> None:
        """resolve_projects with aliases returns only matching projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://a.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))
        store.add_project("dev", ProjectConfig(
            stack_url="https://b.com",
            token="532-abcdef-ghijklmnopqrst",
        ))

        service = ConfigService(config_store=store)
        result = service.resolve_projects(aliases=["prod"])
        assert set(result.keys()) == {"prod"}

    def test_resolve_unknown_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """resolve_projects raises ConfigError for unknown alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ConfigService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.resolve_projects(aliases=["nonexistent"])

    def test_resolve_empty_aliases_list(self, tmp_config_dir: Path) -> None:
        """resolve_projects with empty list returns all projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project("prod", ProjectConfig(
            stack_url="https://a.com",
            token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
        ))

        service = ConfigService(config_store=store)
        result = service.resolve_projects(aliases=[])
        assert set(result.keys()) == {"prod"}
