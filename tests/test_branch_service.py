"""Tests for BranchService - multi-project development branch listing."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.services.branch_service import BranchService

SAMPLE_BRANCHES = [
    {
        "id": 123,
        "name": "main",
        "isDefault": True,
        "created": "2025-01-01T00:00:00Z",
        "description": "Main branch",
    },
    {
        "id": 456,
        "name": "feature-x",
        "isDefault": False,
        "created": "2025-06-15T10:30:00Z",
        "description": "Feature branch",
    },
]

SAMPLE_BRANCHES_DEV = [
    {
        "id": 789,
        "name": "main",
        "isDefault": True,
        "created": "2025-02-01T00:00:00Z",
        "description": "Dev main branch",
    },
]


class TestListBranchesSingleProject:
    """Tests for BranchService.list_branches() with a single project."""

    def test_list_branches_single_project(self, tmp_config_dir: Path) -> None:
        """list_branches returns branches annotated with project alias."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_branches(aliases=["prod"])
        branches = result["branches"]
        errors = result["errors"]

        assert errors == []
        assert len(branches) == 2
        assert branches[0]["project_alias"] == "prod"
        assert branches[0]["id"] == 123
        assert branches[0]["name"] == "main"
        assert branches[0]["isDefault"] is True
        assert branches[1]["id"] == 456
        assert branches[1]["name"] == "feature-x"
        assert branches[1]["isDefault"] is False

    def test_list_branches_empty(self, tmp_config_dir: Path) -> None:
        """list_branches returns empty list when project has no dev branches."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = []

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_branches(aliases=["prod"])
        assert result["branches"] == []
        assert result["errors"] == []


class TestListBranchesMultiProject:
    """Tests for BranchService.list_branches() with multiple projects."""

    def test_list_branches_multi_project(self, tmp_config_dir: Path) -> None:
        """list_branches aggregates branches from all projects."""
        call_count = 0

        def make_client(url: str, token: str) -> MagicMock:
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            if token == "901-xxx":
                mock.list_dev_branches.return_value = SAMPLE_BRANCHES
            else:
                mock.list_dev_branches.return_value = SAMPLE_BRANCHES_DEV
            return mock

        store = setup_two_projects(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=make_client,
        )

        result = svc.list_branches()
        branches = result["branches"]
        errors = result["errors"]

        assert errors == []
        assert len(branches) == 3

        # Branches should be sorted by (project_alias, id)
        dev_branches = [b for b in branches if b["project_alias"] == "dev"]
        prod_branches = [b for b in branches if b["project_alias"] == "prod"]
        assert len(dev_branches) == 1
        assert len(prod_branches) == 2


class TestListBranchesWithError:
    """Tests for error handling in BranchService.list_branches()."""

    def test_list_branches_one_project_fails(self, tmp_config_dir: Path) -> None:
        """When one project fails, the other still returns results."""

        def make_client(url: str, token: str) -> MagicMock:
            mock = MagicMock()
            if token == "901-xxx":
                mock.list_dev_branches.return_value = SAMPLE_BRANCHES
            else:
                mock.list_dev_branches.side_effect = KeboolaApiError(
                    message="Connection refused",
                    error_code="CONNECTION_ERROR",
                    status_code=0,
                    retryable=True,
                )
            return mock

        store = setup_two_projects(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=make_client,
        )

        result = svc.list_branches()
        branches = result["branches"]
        errors = result["errors"]

        assert len(branches) == 2
        assert all(b["project_alias"] == "prod" for b in branches)
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "dev"
        assert errors[0]["error_code"] == "CONNECTION_ERROR"

    def test_list_branches_unknown_alias_raises(self, tmp_config_dir: Path) -> None:
        """Specifying a nonexistent alias raises ConfigError."""
        store = setup_single_project(tmp_config_dir)
        svc = BranchService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'nonexistent' not found"):
            svc.list_branches(aliases=["nonexistent"])

    def test_list_branches_unexpected_error(self, tmp_config_dir: Path) -> None:
        """Unexpected exceptions are captured as UNEXPECTED_ERROR."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.side_effect = RuntimeError("Something broke")

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_branches(aliases=["prod"])
        assert result["branches"] == []
        assert len(result["errors"]) == 1
        assert result["errors"][0]["error_code"] == "UNEXPECTED_ERROR"
        assert "Something broke" in result["errors"][0]["message"]


class TestCreateBranch:
    """Tests for BranchService.create_branch()."""

    def test_create_branch_success(self, tmp_config_dir: Path) -> None:
        """create_branch returns branch data and auto-activates it in config."""
        mock_client = MagicMock()
        # create_dev_branch now waits for async job and returns branch data
        # from job.results (with the real branch ID)
        mock_client.create_dev_branch.return_value = {
            "id": 777,
            "name": "my-feature",
            "description": "A feature branch",
            "created": "2025-07-01T12:00:00Z",
        }

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.create_branch(alias="prod", name="my-feature", description="A feature branch")

        assert result["project_alias"] == "prod"
        assert result["branch_id"] == 777
        assert result["branch_name"] == "my-feature"
        assert result["activated"] is True

        # Verify auto-activation persisted in config
        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 777

        mock_client.create_dev_branch.assert_called_once_with(
            name="my-feature", description="A feature branch"
        )

    def test_create_branch_unknown_project(self, tmp_config_dir: Path) -> None:
        """create_branch raises ConfigError for an unknown project alias."""
        store = setup_single_project(tmp_config_dir)
        svc = BranchService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'nonexistent' not found"):
            svc.create_branch(alias="nonexistent", name="some-branch")

    def test_create_branch_api_error(self, tmp_config_dir: Path) -> None:
        """create_branch propagates KeboolaApiError from the client."""
        mock_client = MagicMock()
        mock_client.create_dev_branch.side_effect = KeboolaApiError(
            message="Branch name already exists",
            error_code="CONFLICT",
            status_code=409,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Branch name already exists"):
            svc.create_branch(alias="prod", name="duplicate-branch")


class TestSetActiveBranch:
    """Tests for BranchService.set_active_branch()."""

    def test_set_active_branch_success(self, tmp_config_dir: Path) -> None:
        """set_active_branch validates branch exists and stores it in config."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.set_active_branch(alias="prod", branch_id=456)

        assert result["project_alias"] == "prod"
        assert result["branch_id"] == 456
        assert result["branch_name"] == "feature-x"

        # Verify config was updated
        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 456

    def test_set_active_branch_not_found(self, tmp_config_dir: Path) -> None:
        """set_active_branch raises ConfigError when branch ID does not exist."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(ConfigError, match="Branch ID 999 not found"):
            svc.set_active_branch(alias="prod", branch_id=999)

    def test_set_active_branch_api_error(self, tmp_config_dir: Path) -> None:
        """set_active_branch propagates KeboolaApiError from the client."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.side_effect = KeboolaApiError(
            message="Forbidden",
            error_code="AUTH_ERROR",
            status_code=403,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Forbidden"):
            svc.set_active_branch(alias="prod", branch_id=456)


class TestResetBranch:
    """Tests for BranchService.reset_branch()."""

    def test_reset_branch_success(self, tmp_config_dir: Path) -> None:
        """reset_branch clears the active branch, reverting to main."""
        store = setup_single_project(tmp_config_dir)

        # Set an active branch first
        store.set_project_branch("prod", 456)
        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 456

        svc = BranchService(config_store=store)

        result = svc.reset_branch(alias="prod")

        assert result["project_alias"] == "prod"
        assert result["previous_branch_id"] == 456

        # Verify config was cleared
        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id is None

    def test_reset_branch_unknown_project(self, tmp_config_dir: Path) -> None:
        """reset_branch raises ConfigError for an unknown project alias."""
        store = setup_single_project(tmp_config_dir)
        svc = BranchService(config_store=store)

        with pytest.raises(ConfigError, match="Project 'ghost' not found"):
            svc.reset_branch(alias="ghost")


class TestDeleteBranch:
    """Tests for BranchService.delete_branch()."""

    def test_delete_branch_success(self, tmp_config_dir: Path) -> None:
        """delete_branch calls API and returns confirmation."""
        mock_client = MagicMock()

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.delete_branch(alias="prod", branch_id=456)

        assert result["project_alias"] == "prod"
        assert result["branch_id"] == 456
        assert result["was_active"] is False
        mock_client.delete_dev_branch.assert_called_once_with(456)

    def test_delete_branch_auto_reset(self, tmp_config_dir: Path) -> None:
        """delete_branch resets active_branch_id when deleting the active branch."""
        mock_client = MagicMock()

        store = setup_single_project(tmp_config_dir)
        # Set the branch as active first
        store.set_project_branch("prod", 456)

        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.delete_branch(alias="prod", branch_id=456)

        assert result["was_active"] is True
        assert "reset to main" in result["message"]

        # Verify active branch was cleared
        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id is None

    def test_delete_branch_api_error(self, tmp_config_dir: Path) -> None:
        """delete_branch propagates KeboolaApiError from the client."""
        mock_client = MagicMock()
        mock_client.delete_dev_branch.side_effect = KeboolaApiError(
            message="Branch not found",
            error_code="NOT_FOUND",
            status_code=404,
            retryable=False,
        )

        store = setup_single_project(tmp_config_dir)
        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError, match="Branch not found"):
            svc.delete_branch(alias="prod", branch_id=999)


class TestListBranchesActiveBranches:
    """Tests for active_branches key in list_branches() response."""

    def test_list_branches_includes_active_branches(self, tmp_config_dir: Path) -> None:
        """list_branches result contains an active_branches dict mapping alias to branch ID."""
        mock_client = MagicMock()
        mock_client.list_dev_branches.return_value = SAMPLE_BRANCHES

        store = setup_single_project(tmp_config_dir)
        # Set an active branch
        store.set_project_branch("prod", 456)

        svc = BranchService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = svc.list_branches(aliases=["prod"])

        assert "active_branches" in result
        assert result["active_branches"] == {"prod": 456}
