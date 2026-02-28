"""Shared test helper functions for Keboola Agent CLI tests.

Contains factory functions for creating mock clients and pre-configured
ConfigStore instances. Used across multiple test files to avoid duplication.
"""

from pathlib import Path
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse


def make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
    token_description: str = "My Token",
) -> MagicMock:
    """Create a mock KeboolaClient that returns a successful verify_token response.

    Used by test_cli.py, test_services.py, and other test files that need
    a mock client with a working verify_token.
    """
    mock_client = MagicMock()
    mock_client.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description=token_description,
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return mock_client


def make_failing_client(error: KeboolaApiError) -> MagicMock:
    """Create a mock KeboolaClient whose verify_token raises the given error."""
    mock_client = MagicMock()
    mock_client.verify_token.side_effect = error
    return mock_client


def setup_single_project(
    tmp_config_dir: Path,
    alias: str = "prod",
    stack_url: str = "https://connection.keboola.com",
    token: str = "901-xxx",
    project_name: str = "Production",
    project_id: int = 258,
) -> ConfigStore:
    """Create a ConfigStore with a single project configured.

    Used by test_base_service.py, test_lineage_service.py, and other test files
    that need a pre-configured ConfigStore with one project.
    """
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url=stack_url,
            token=token,
            project_name=project_name,
            project_id=project_id,
        ),
    )
    return store


def setup_two_projects(tmp_config_dir: Path) -> ConfigStore:
    """Create a ConfigStore with two projects (prod and dev) configured.

    Used by test_base_service.py, test_lineage_service.py, and other test files
    that need a pre-configured ConfigStore with two projects.
    """
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        "prod",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-xxx",
            project_name="Production",
            project_id=258,
        ),
    )
    store.add_project(
        "dev",
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="7012-yyy",
            project_name="Development",
            project_id=7012,
        ),
    )
    return store
