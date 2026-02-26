"""Tests for ProjectService - add, remove, edit, list, status."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
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
