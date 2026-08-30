"""Shared test helper functions for Keboola Agent CLI tests.

Contains factory functions for creating mock clients and pre-configured
ConfigStore instances. Used across multiple test files to avoid duplication.
"""

from pathlib import Path
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse


def metastore_scope_available(url: str, token: str) -> bool:
    """Probe whether a project has a usable metastore scope (E2E preflight).

    "Failed to create project scope" is the metastore's blanket answer when it
    cannot build a project scope for the caller -- most commonly its
    master-token gate rejecting a valid non-master token with a 401 (issue
    #711; kbagent reclassifies that to ``MISSING_MASTER_TOKEN``), historically
    also seen as a 502 on some deployments. Either way it is an environment
    limitation, not a test failure. The semantic-layer E2E suites call this
    and ``pytest.skip()`` when it returns False, so they skip cleanly instead
    of reporting a wall of false-positive failures.
    """
    from keboola_agent_cli.metastore_client import SEMANTIC_TYPES, MetastoreClient

    try:
        with MetastoreClient(stack_url=url, token=token) as mc:
            mc.list_items(SEMANTIC_TYPES[0])  # ty: ignore[invalid-argument-type]  # probe; str vs SemanticType Literal
        return True
    except KeboolaApiError as exc:
        # Skip cleanly when the scope is genuinely unavailable (502 / "scope")
        # OR the metastore host is simply unreachable (network / DNS -- e.g. a
        # malformed or accidentally doubled stack URL). Both mean "no usable
        # metastore here"; raising would turn one preflight failure into a wall
        # of errors across every dependent test.
        msg = (exc.message or "").lower()
        if exc.status_code == 502 or "scope" in msg or "cannot connect" in msg:
            return False
        raise


def make_mock_client(
    project_name: str = "Test Project",
    project_id: int = 1234,
    token_description: str = "My Token",
    org_id: int | None = None,
    org_name: str | None = None,
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
        org_id=org_id,
        org_name=org_name,
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
