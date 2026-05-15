"""Tests for OrgService - organization setup, slugify, alias uniqueness."""

from pathlib import Path
from unittest.mock import MagicMock

from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import KeboolaApiError
from keboola_agent_cli.models import ProjectConfig, TokenVerifyResponse
from keboola_agent_cli.services.org_service import OrgService, slugify


class TestSlugify:
    """Tests for the slugify() function."""

    def test_basic_name(self) -> None:
        assert slugify("My Project") == "my-project"

    def test_with_special_chars(self) -> None:
        assert slugify("My Project (v2)") == "my-project-v2"

    def test_with_slashes(self) -> None:
        assert slugify("Prod / Main") == "prod-main"

    def test_extra_whitespace(self) -> None:
        assert slugify("  Hello   World  ") == "hello-world"

    def test_leading_trailing_hyphens(self) -> None:
        assert slugify("---test---") == "test"

    def test_empty_string(self) -> None:
        assert slugify("") == "project"

    def test_only_special_chars(self) -> None:
        assert slugify("@#$%") == "project"

    def test_numbers(self) -> None:
        assert slugify("Project 123") == "project-123"

    def test_unicode_fallback(self) -> None:
        # Non-ASCII chars are replaced with hyphens
        assert slugify("Projekt-CZ") == "projekt-cz"

    def test_single_word(self) -> None:
        assert slugify("production") == "production"


def _make_manage_client(projects: list[dict], token_response: dict | None = None):
    """Create a mock manage client factory."""
    mock = MagicMock()
    mock.list_organization_projects.return_value = projects
    if token_response:
        mock.create_project_token.return_value = token_response
    else:
        mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }
    return lambda url, token: mock


def _make_storage_client(project_name: str = "Test Project", project_id: int = 100):
    """Create a mock storage client factory."""
    mock = MagicMock()
    mock.verify_token.return_value = TokenVerifyResponse(
        token_id="12345",
        token_description="kbagent-cli",
        project_id=project_id,
        project_name=project_name,
        owner_name=project_name,
    )
    return lambda url, token: mock


class TestSetupOrganization:
    """Tests for OrgService.setup_organization()."""

    def test_dry_run_preview(self, tmp_path: Path) -> None:
        """Dry run returns preview without side effects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [
            {"id": 100, "name": "Alpha"},
            {"id": 200, "name": "Beta"},
        ]
        manage_factory = _make_manage_client(projects)

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=_make_storage_client(),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["organization_id"] == 42
        assert result["projects_found"] == 2
        assert len(result["projects_added"]) == 2
        assert result["projects_added"][0]["action"] == "would_add"
        assert result["projects_added"][0]["alias"] == "alpha"
        assert result["projects_added"][1]["alias"] == "beta"
        assert len(result["projects_skipped"]) == 0
        assert len(result["projects_failed"]) == 0

        # No projects should be in config (dry run)
        config = store.load()
        assert len(config.projects) == 0

    def test_add_new_projects(self, tmp_path: Path) -> None:
        """Successfully adds new projects to config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [
            {"id": 100, "name": "Alpha"},
            {"id": 200, "name": "Beta"},
        ]

        # Create per-project storage client mocks
        storage_mocks = {}

        def storage_factory(url, token):
            mock = MagicMock()
            # Use project_id=100 for first token, 200 for second
            pid = 100 if "100" not in str(storage_mocks) or len(storage_mocks) == 0 else 200
            if len(storage_mocks) == 0:
                pid = 100
                name = "Alpha"
            else:
                pid = 200
                name = "Beta"
            mock.verify_token.return_value = TokenVerifyResponse(
                token_id="tok-1",
                token_description="kbagent-cli",
                project_id=pid,
                project_name=name,
                owner_name=name,
            )
            storage_mocks[len(storage_mocks)] = mock
            return mock

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client(projects),
            storage_client_factory=storage_factory,
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        assert result["dry_run"] is False
        assert len(result["projects_added"]) == 2
        assert result["projects_added"][0]["action"] == "added"
        assert len(result["projects_skipped"]) == 0
        assert len(result["projects_failed"]) == 0

        # Verify projects are in config
        config = store.load()
        assert len(config.projects) == 2
        assert "alpha" in config.projects
        assert "beta" in config.projects

    def test_populates_org_name_from_per_project_payload(self, tmp_path: Path) -> None:
        """When list_organization_projects payload carries organization.name, use it."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [
            {
                "id": 100,
                "name": "Alpha",
                "organization": {"id": 42, "name": "Acme Corp"},
            },
        ]

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client(projects),
            storage_client_factory=_make_storage_client(project_name="Alpha", project_id=100),
        )

        service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        registered = store.load().projects["alpha"]
        assert registered.org_id == 42
        assert registered.org_name == "Acme Corp"

    def test_falls_back_to_get_organization_for_name(self, tmp_path: Path) -> None:
        """When per-project payload omits org name, fetch it via get_organization."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [{"id": 100, "name": "Alpha"}]  # no organization sub-object

        manage_mock = MagicMock()
        manage_mock.list_organization_projects.return_value = projects
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }
        manage_mock.get_organization.return_value = {"id": 42, "name": "Resolved Org"}

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(project_name="Alpha", project_id=100),
        )

        service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        manage_mock.get_organization.assert_called_once_with(42)
        registered = store.load().projects["alpha"]
        assert registered.org_id == 42
        assert registered.org_name == "Resolved Org"

    def test_skip_existing_projects(self, tmp_path: Path) -> None:
        """Already-registered projects are skipped."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        # Pre-register project 100
        store.add_project(
            "alpha",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-existing-tokenValue1234567890",
                project_name="Alpha",
                project_id=100,
            ),
        )

        projects = [
            {"id": 100, "name": "Alpha"},
            {"id": 200, "name": "Beta"},
        ]

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client(projects),
            storage_client_factory=_make_storage_client(project_name="Beta", project_id=200),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        assert len(result["projects_added"]) == 1
        assert result["projects_added"][0]["alias"] == "beta"
        assert len(result["projects_skipped"]) == 1
        assert result["projects_skipped"][0]["project_id"] == 100
        assert result["projects_skipped"][0]["reason"] == "Already registered in config"

    def test_error_accumulation(self, tmp_path: Path) -> None:
        """One project failing doesn't stop processing of others."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [
            {"id": 100, "name": "Alpha"},
            {"id": 200, "name": "Beta"},
            {"id": 300, "name": "Gamma"},
        ]

        call_count = {"n": 0}

        def manage_factory(url, token):
            mock = MagicMock()
            mock.list_organization_projects.return_value = projects

            def create_token(project_id, description, **kwargs):
                call_count["n"] += 1
                if project_id == 200:
                    raise KeboolaApiError(
                        message="Access denied to project 200",
                        status_code=403,
                        error_code="ACCESS_DENIED",
                    )
                return {
                    "id": f"tok-{project_id}",
                    "token": f"901-{project_id}-generatedTokenValue12345",
                    "description": description,
                }

            mock.create_project_token.side_effect = create_token
            return mock

        storage_call_count = {"n": 0}

        def storage_factory(url, token):
            storage_call_count["n"] += 1
            mock = MagicMock()
            # Determine project based on token
            if "100" in token:
                pid, name = 100, "Alpha"
            else:
                pid, name = 300, "Gamma"
            mock.verify_token.return_value = TokenVerifyResponse(
                token_id="tok-1",
                token_description="kbagent-cli",
                project_id=pid,
                project_name=name,
                owner_name=name,
            )
            return mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        assert len(result["projects_added"]) == 2
        assert len(result["projects_failed"]) == 1
        assert result["projects_failed"][0]["project_id"] == 200
        assert "Access denied" in result["projects_failed"][0]["error"]

    def test_unique_alias_generation(self, tmp_path: Path) -> None:
        """Generates unique aliases when project names collide."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [
            {"id": 100, "name": "Production"},
            {"id": 200, "name": "Production"},
            {"id": 300, "name": "Production"},
        ]

        call_idx = {"n": 0}

        def storage_factory(url, token):
            mock = MagicMock()
            call_idx["n"] += 1
            pid = [100, 200, 300][min(call_idx["n"] - 1, 2)]
            mock.verify_token.return_value = TokenVerifyResponse(
                token_id="tok-1",
                token_description="kbagent-cli",
                project_id=pid,
                project_name="Production",
                owner_name="Production",
            )
            return mock

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client(projects),
            storage_client_factory=storage_factory,
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        aliases = [p["alias"] for p in result["projects_added"]]
        assert aliases == ["production", "production-2", "production-3"]
        assert len(set(aliases)) == 3  # All unique

    def test_empty_organization(self, tmp_path: Path) -> None:
        """Returns empty results for organization with no projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client([]),
            storage_client_factory=_make_storage_client(),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        assert result["projects_found"] == 0
        assert len(result["projects_added"]) == 0
        assert len(result["projects_skipped"]) == 0
        assert len(result["projects_failed"]) == 0

    def test_custom_token_description(self, tmp_path: Path) -> None:
        """Custom token description is passed to manage client."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [{"id": 100, "name": "Alpha"}]

        manage_mock = MagicMock()
        manage_mock.list_organization_projects.return_value = projects
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "my-custom-prefix (Alpha)",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(),
        )

        service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
            token_description="my-custom-prefix",
        )

        # Verify the description was passed correctly
        call_args = manage_mock.create_project_token.call_args
        assert "my-custom-prefix (Alpha)" in call_args.kwargs.get(
            "description", call_args[1].get("description", "")
        ) or "my-custom-prefix" in str(call_args)


class TestTokenExpiration:
    """Tests for token expiration (expiresIn) parameter."""

    def test_expires_in_passed_to_manage_client(self, tmp_path: Path) -> None:
        """When token_expires_in is set, it is forwarded to create_project_token."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [{"id": 100, "name": "Alpha"}]

        manage_mock = MagicMock()
        manage_mock.list_organization_projects.return_value = projects
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(),
        )

        service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
            token_expires_in=3600,
        )

        call_kwargs = manage_mock.create_project_token.call_args.kwargs
        assert call_kwargs["expires_in"] == 3600

    def test_expires_in_none_not_in_payload(self, tmp_path: Path) -> None:
        """When token_expires_in is not set, expiresIn is absent from API payload."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        projects = [{"id": 100, "name": "Alpha"}]

        manage_mock = MagicMock()
        manage_mock.list_organization_projects.return_value = projects
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(),
        )

        service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        # Verify expires_in=None was passed (ManageClient will skip expiresIn from payload)
        call_kwargs = manage_mock.create_project_token.call_args.kwargs
        assert call_kwargs["expires_in"] is None


class TestExistingProjectIdNone:
    """Tests for existing projects with project_id=None not polluting the set."""

    def test_none_project_id_not_in_existing_set(self, tmp_path: Path) -> None:
        """A pre-registered project with project_id=None does not block new projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        # Pre-register a project without project_id (defaults to None)
        store.add_project(
            "legacy",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-legacy-tokenValue12345678901",
                project_name="Legacy",
                # project_id is None
            ),
        )

        projects = [{"id": 100, "name": "Alpha"}]

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client(projects),
            storage_client_factory=_make_storage_client(),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="manage-token-123456789012345678",
            org_id=42,
        )

        # Project 100 should be added, not skipped
        assert len(result["projects_added"]) == 1
        assert result["projects_added"][0]["project_id"] == 100
        assert len(result["projects_skipped"]) == 0


class TestSetupWithProjectIds:
    """Tests for OrgService.setup_organization() with explicit project_ids (non-admin mode)."""

    def test_dry_run_with_project_ids(self, tmp_path: Path) -> None:
        """Dry run fetches each project individually and returns preview."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        manage_mock = MagicMock()
        manage_mock.get_project.side_effect = [
            {"id": 901, "name": "Padak", "organization": {"id": 438}},
            {"id": 9621, "name": "Padak - BQ/GCS", "organization": {"id": 438}},
        ]

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="pat-token-123456789012345678901",
            project_ids=[901, 9621],
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert result["projects_found"] == 2
        assert result["organization_id"] == 438
        assert len(result["projects_added"]) == 2
        assert result["projects_added"][0]["alias"] == "padak"
        assert result["projects_added"][1]["alias"] == "padak-bq-gcs"

        # get_project called for each ID, NOT list_organization_projects
        assert manage_mock.get_project.call_count == 2
        manage_mock.list_organization_projects.assert_not_called()

    def test_add_projects_by_ids(self, tmp_path: Path) -> None:
        """Successfully adds projects using explicit project IDs."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        manage_mock = MagicMock()
        manage_mock.get_project.return_value = {
            "id": 901,
            "name": "Padak",
            "organization": {"id": 438},
        }
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(project_name="Padak", project_id=901),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="pat-token-123456789012345678901",
            project_ids=[901],
        )

        assert len(result["projects_added"]) == 1
        assert result["projects_added"][0]["action"] == "added"
        assert result["projects_added"][0]["alias"] == "padak"
        assert result["organization_id"] == 438

        config = store.load()
        assert "padak" in config.projects

    def test_inaccessible_project_in_fetch(self, tmp_path: Path) -> None:
        """Projects the user can't access are reported as failed."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        manage_mock = MagicMock()
        manage_mock.get_project.side_effect = [
            {"id": 901, "name": "Padak", "organization": {"id": 438}},
            KeboolaApiError(
                message="Access denied to project 999",
                status_code=403,
                error_code="ACCESS_DENIED",
            ),
        ]
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(project_name="Padak", project_id=901),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="pat-token-123456789012345678901",
            project_ids=[901, 999],
        )

        assert len(result["projects_added"]) == 1
        assert len(result["projects_failed"]) == 1
        assert result["projects_failed"][0]["project_id"] == 999
        assert "Access denied" in result["projects_failed"][0]["error"]

    def test_no_org_id_no_project_ids_raises(self, tmp_path: Path) -> None:
        """Raises ValueError when neither org_id nor project_ids is provided."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        service = OrgService(
            config_store=store,
            manage_client_factory=_make_manage_client([]),
            storage_client_factory=_make_storage_client(),
        )

        import pytest as pt

        with pt.raises(ValueError, match="Either org_id or project_ids"):
            service.setup_organization(
                stack_url="https://connection.keboola.com",
                manage_token="pat-token-123456789012345678901",
            )

    def test_org_id_derived_from_first_project(self, tmp_path: Path) -> None:
        """When no org_id given, it is derived from the first fetched project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        manage_mock = MagicMock()
        manage_mock.get_project.return_value = {
            "id": 901,
            "name": "Padak",
            "organization": {"id": 438},
        }
        manage_mock.create_project_token.return_value = {
            "id": "tok-1",
            "token": "901-99999-generatedToken1234567890ab",
            "description": "kbagent-cli",
        }

        service = OrgService(
            config_store=store,
            manage_client_factory=lambda url, token: manage_mock,
            storage_client_factory=_make_storage_client(project_name="Padak", project_id=901),
        )

        result = service.setup_organization(
            stack_url="https://connection.keboola.com",
            manage_token="pat-token-123456789012345678901",
            project_ids=[901],
        )

        assert result["organization_id"] == 438


class TestUniqueAlias:
    """Tests for OrgService._unique_alias() static method."""

    def test_no_collision(self) -> None:
        assert OrgService._unique_alias("prod", set()) == "prod"

    def test_single_collision(self) -> None:
        assert OrgService._unique_alias("prod", {"prod"}) == "prod-2"

    def test_multiple_collisions(self) -> None:
        assert OrgService._unique_alias("prod", {"prod", "prod-2"}) == "prod-3"

    def test_gap_in_sequence(self) -> None:
        assert OrgService._unique_alias("prod", {"prod", "prod-2", "prod-3"}) == "prod-4"


class TestRefreshTokens:
    """Tests for OrgService.refresh_tokens()."""

    @staticmethod
    def _setup_store(tmp_path: Path, projects: dict[str, dict]) -> ConfigStore:
        """Create a ConfigStore with the given projects pre-registered.

        Args:
            tmp_path: Temporary directory for config files.
            projects: Mapping of alias -> project kwargs for ProjectConfig.

        Returns:
            ConfigStore with all projects added.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        for alias, kwargs in projects.items():
            store.add_project(alias, ProjectConfig(**kwargs))
        return store

    @staticmethod
    def _make_manage_mock(
        token_response: dict | None = None,
        verify_response: dict | None = None,
    ) -> MagicMock:
        """Create a mock ManageClient for refresh operations.

        Args:
            token_response: Response from create_project_token.
            verify_response: Response from verify_token (manage token identity).

        Returns:
            MagicMock configured as ManageClient.
        """
        mock = MagicMock()
        mock.create_project_token.return_value = token_response or {
            "id": "tok-new",
            "token": "901-99999-newGeneratedTokenValue12345",
            "description": "kbagent-cli",
        }
        mock.verify_token.return_value = verify_response or {
            "user": {"email": "admin@test.com", "name": "Admin"},
        }
        return mock

    def test_refresh_single_invalid_token(self, tmp_path: Path) -> None:
        """An invalid token is detected and refreshed with a new one."""
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-old-expiredTokenValue123456789",
                    "project_name": "Prod",
                    "project_id": 100,
                },
            },
        )

        # Storage client: first call (old token check) fails, second call (new token verify) succeeds
        mock_storage = MagicMock()
        mock_storage.verify_token.side_effect = [
            KeboolaApiError(
                message="Invalid token",
                status_code=401,
                error_code="INVALID_TOKEN",
            ),
            TokenVerifyResponse(
                token_id="new-123",
                token_description="kbagent-cli",
                project_id=100,
                project_name="Prod",
                owner_name="Prod",
            ),
        ]

        def storage_factory(url: str, token: str) -> MagicMock:
            return mock_storage

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
        )

        assert result["projects_checked"] == 1
        assert len(result["projects_refreshed"]) == 1
        assert result["projects_refreshed"][0]["alias"] == "prod"
        assert result["projects_refreshed"][0]["action"] == "refreshed"
        assert result["projects_refreshed"][0]["project_id"] == 100
        assert len(result["projects_valid"]) == 0
        assert len(result["projects_failed"]) == 0

        # Config should be updated with the new token
        config = store.load()
        assert config.projects["prod"].token == "901-99999-newGeneratedTokenValue12345"

    def test_refresh_all_only_invalid(self, tmp_path: Path) -> None:
        """With two projects, only the one with an invalid token is refreshed."""
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-prod-validTokenValue1234567890",
                    "project_name": "Prod",
                    "project_id": 100,
                },
                "dev": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-dev-expiredTokenValue123456789",
                    "project_name": "Dev",
                    "project_id": 200,
                },
            },
        )

        # We need per-project storage mocks since each project gets its own client
        call_count = {"n": 0}

        def storage_factory(url, token):
            call_count["n"] += 1
            mock = MagicMock()
            if "prod-valid" in token:
                # Prod: valid token
                mock.verify_token.return_value = TokenVerifyResponse(
                    token_id="prod-tok",
                    token_description="kbagent-cli",
                    project_id=100,
                    project_name="Prod",
                    owner_name="Prod",
                )
            elif "dev-expired" in token:
                # Dev: invalid old token
                mock.verify_token.side_effect = KeboolaApiError(
                    message="Invalid token",
                    status_code=401,
                    error_code="INVALID_TOKEN",
                )
            else:
                # New token verification (after refresh)
                mock.verify_token.return_value = TokenVerifyResponse(
                    token_id="new-dev-tok",
                    token_description="kbagent-cli",
                    project_id=200,
                    project_name="Dev",
                    owner_name="Dev",
                )
            return mock

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
        )

        assert result["projects_checked"] == 2
        assert len(result["projects_valid"]) == 1
        assert result["projects_valid"][0]["alias"] == "prod"
        assert len(result["projects_refreshed"]) == 1
        assert result["projects_refreshed"][0]["alias"] == "dev"
        assert result["projects_refreshed"][0]["action"] == "refreshed"
        assert len(result["projects_failed"]) == 0

    def test_refresh_dry_run(self, tmp_path: Path) -> None:
        """Dry run reports what would be refreshed without making changes."""
        old_token = "901-old-expiredTokenValue123456789"
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": old_token,
                    "project_name": "Prod",
                    "project_id": 100,
                },
            },
        )

        mock_storage = MagicMock()
        mock_storage.verify_token.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
        )

        def storage_factory(url: str, token: str) -> MagicMock:
            return mock_storage

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
            dry_run=True,
        )

        assert result["dry_run"] is True
        assert len(result["projects_refreshed"]) == 1
        assert result["projects_refreshed"][0]["action"] == "would_refresh"
        assert result["projects_refreshed"][0]["token"] == "***"

        # Config should NOT be updated
        config = store.load()
        assert config.projects["prod"].token == old_token

        # create_project_token should NOT have been called
        manage_mock.create_project_token.assert_not_called()

    def test_refresh_force_valid_token(self, tmp_path: Path) -> None:
        """force=True refreshes even valid tokens."""
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-prod-validTokenValue1234567890",
                    "project_name": "Prod",
                    "project_id": 100,
                },
            },
        )

        # Storage: first call (check) succeeds, second call (verify new) also succeeds
        call_count = {"n": 0}

        def storage_factory(url, token):
            call_count["n"] += 1
            mock = MagicMock()
            mock.verify_token.return_value = TokenVerifyResponse(
                token_id=f"tok-{call_count['n']}",
                token_description="kbagent-cli",
                project_id=100,
                project_name="Prod",
                owner_name="Prod",
            )
            return mock

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
            force=True,
        )

        assert result["projects_checked"] == 1
        assert len(result["projects_refreshed"]) == 1
        assert result["projects_refreshed"][0]["alias"] == "prod"
        assert result["projects_refreshed"][0]["action"] == "refreshed"
        assert len(result["projects_valid"]) == 0

        # create_project_token should have been called despite valid token
        manage_mock.create_project_token.assert_called_once()

        # Config should be updated with the new token
        config = store.load()
        assert config.projects["prod"].token == "901-99999-newGeneratedTokenValue12345"

    def test_refresh_skip_no_project_id(self, tmp_path: Path) -> None:
        """Projects without project_id are skipped with explanation."""
        store = self._setup_store(
            tmp_path,
            {
                "legacy": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-legacy-tokenValue12345678901",
                    "project_name": "Legacy",
                    # project_id defaults to None
                },
            },
        )

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        mock_storage = MagicMock()

        def storage_factory(url: str, token: str) -> MagicMock:
            return mock_storage

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
        )

        assert result["projects_checked"] == 1
        assert len(result["projects_skipped"]) == 1
        assert result["projects_skipped"][0]["alias"] == "legacy"
        assert "project_id is missing" in result["projects_skipped"][0]["reason"]
        assert len(result["projects_refreshed"]) == 0
        assert len(result["projects_valid"]) == 0

        # Storage verify_token should NOT have been called (skipped before check)
        mock_storage.verify_token.assert_not_called()

    def test_refresh_partial_failure(self, tmp_path: Path) -> None:
        """One project refreshing successfully while another fails."""
        store = self._setup_store(
            tmp_path,
            {
                "alpha": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-alpha-expiredTokenValue12345",
                    "project_name": "Alpha",
                    "project_id": 100,
                },
                "beta": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-beta-expiredTokenValue123456",
                    "project_name": "Beta",
                    "project_id": 200,
                },
            },
        )

        # Storage: all old tokens are invalid, new token verification succeeds
        def storage_factory(url, token):
            mock = MagicMock()
            if "expired" in token:
                mock.verify_token.side_effect = KeboolaApiError(
                    message="Invalid token",
                    status_code=401,
                    error_code="INVALID_TOKEN",
                )
            else:
                mock.verify_token.return_value = TokenVerifyResponse(
                    token_id="new-tok",
                    token_description="kbagent-cli",
                    project_id=100,
                    project_name="Alpha",
                    owner_name="Alpha",
                )
            return mock

        # Manage: first create_project_token succeeds, second fails
        create_call_count = {"n": 0}

        def manage_factory(url, token):
            mock = MagicMock()
            mock.verify_token.return_value = {
                "user": {"email": "admin@test.com", "name": "Admin"},
            }

            def create_token(project_id, description, **kwargs):
                create_call_count["n"] += 1
                if project_id == 200:
                    raise KeboolaApiError(
                        message="Access denied to project 200",
                        status_code=403,
                        error_code="ACCESS_DENIED",
                    )
                return {
                    "id": "tok-new",
                    "token": "901-99999-newGeneratedTokenValue12345",
                    "description": description,
                }

            mock.create_project_token.side_effect = create_token
            return mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
        )

        assert result["projects_checked"] == 2
        assert len(result["projects_refreshed"]) == 1
        assert result["projects_refreshed"][0]["alias"] == "alpha"
        assert len(result["projects_failed"]) == 1
        assert result["projects_failed"][0]["alias"] == "beta"
        assert "Access denied" in result["projects_failed"][0]["error"]

    def test_refresh_all_valid_no_action(self, tmp_path: Path) -> None:
        """All valid tokens result in no refreshes."""
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-prod-validTokenValue1234567890",
                    "project_name": "Prod",
                    "project_id": 100,
                },
                "dev": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-dev-validTokenValue12345678901",
                    "project_name": "Dev",
                    "project_id": 200,
                },
            },
        )

        def storage_factory(url, token):
            mock = MagicMock()
            if "prod-valid" in token:
                pid, name = 100, "Prod"
            else:
                pid, name = 200, "Dev"
            mock.verify_token.return_value = TokenVerifyResponse(
                token_id=f"tok-{pid}",
                token_description="kbagent-cli",
                project_id=pid,
                project_name=name,
                owner_name=name,
            )
            return mock

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
        )

        assert result["projects_checked"] == 2
        assert len(result["projects_valid"]) == 2
        assert len(result["projects_refreshed"]) == 0
        assert len(result["projects_failed"]) == 0
        assert len(result["projects_skipped"]) == 0

        # create_project_token should NOT have been called
        manage_mock.create_project_token.assert_not_called()

    def test_refresh_unknown_alias(self, tmp_path: Path) -> None:
        """Requesting an unknown alias reports it as failed."""
        store = self._setup_store(
            tmp_path,
            {
                "prod": {
                    "stack_url": "https://connection.keboola.com",
                    "token": "901-prod-validTokenValue1234567890",
                    "project_name": "Prod",
                    "project_id": 100,
                },
            },
        )

        manage_mock = self._make_manage_mock()

        def manage_factory(url: str, token: str) -> MagicMock:
            return manage_mock

        mock_storage = MagicMock()

        def storage_factory(url: str, token: str) -> MagicMock:
            return mock_storage

        service = OrgService(
            config_store=store,
            manage_client_factory=manage_factory,
            storage_client_factory=storage_factory,
        )

        result = service.refresh_tokens(
            manage_token="manage-token-123456789012345678",
            aliases=["nonexistent"],
        )

        assert len(result["projects_failed"]) == 1
        assert result["projects_failed"][0]["alias"] == "nonexistent"
        assert "not found in config" in result["projects_failed"][0]["error"]
        assert result["projects_checked"] == 0
        assert len(result["projects_refreshed"]) == 0
        assert len(result["projects_valid"]) == 0
