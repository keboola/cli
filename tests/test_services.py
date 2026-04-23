"""Tests for ProjectService and ConfigService."""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from helpers import make_failing_client, make_mock_client
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.models import ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.project_service import ProjectService


class TestAddProject:
    """Tests for ProjectService.add_project()."""

    def test_add_project_success(self, tmp_config_dir: Path) -> None:
        """add_project verifies token, saves to config, returns project info."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = make_mock_client(project_name="Production", project_id=9999)

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
        mock_client = make_failing_client(
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
        mock_client = make_mock_client()

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
        mock_client = make_failing_client(
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
        mock_client = make_mock_client()
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
        mock_client = make_mock_client()

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
        initial_client = make_mock_client(project_name="Old Project", project_id=1000)
        new_client = make_mock_client(project_name="New Project", project_id=2000)

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
        mock_client = make_mock_client()
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
        mock_client = make_mock_client()
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
        mock_client = make_mock_client()
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
        assert result[0]["token"] == "901-...pt0k"


class TestGetStatus:
    """Tests for ProjectService.get_status()."""

    def test_status_all_ok(self, tmp_config_dir: Path) -> None:
        """get_status returns OK status with response time for healthy projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        mock_client = make_mock_client(project_name="Production", project_id=1234)
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

        ok_client = make_mock_client(project_name="OK Project")
        fail_client = make_failing_client(
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

        store.add_project(
            "ok-project",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-ok-abcdefghijklmnop",
                project_name="OK",
                project_id=1,
            ),
        )
        store.add_project(
            "bad-project",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="902-bad-abcdefghijklmnop",
                project_name="Bad",
                project_id=2,
            ),
        )

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
        mock_client = make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        store.add_project(
            "first",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-abcdef-12345678",
            ),
        )
        store.add_project(
            "second",
            ProjectConfig(
                stack_url="https://b.com",
                token="902-abcdef-12345678",
            ),
        )

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
        mock_client = make_mock_client()
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=full_token,
            ),
        )

        result = service.get_status()
        assert result[0]["token"] != full_token
        assert result[0]["token"] == "901-...pt0k"


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


class TestUseAndCurrentProject:
    """Tests for ProjectService.use_project() / current_project() / resolve_pinned_alias()."""

    def _seed_two(self, tmp_config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=tmp_config_dir)
        for alias, pid in (("prod", 1), ("stage", 2)):
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=f"901-x-{alias}",
                    project_name=alias.title(),
                    project_id=pid,
                ),
            )
        return store

    def test_use_project_pins_and_persists(self, tmp_config_dir: Path) -> None:
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.use_project(alias="stage")

        assert result["alias"] == "stage"
        assert result["previous"] == "prod"
        assert result["source"] == "pin"
        # Persistence check
        assert ConfigStore(config_dir=tmp_config_dir).load().default_project == "stage"

    def test_use_project_unknown_raises(self, tmp_config_dir: Path) -> None:
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="not found"):
            service.use_project(alias="does-not-exist")

    def test_current_project_pin_only(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.current_project()
        assert result["alias"] == "prod"
        assert result["source"] == "pin"
        assert result["env_override"] is None

    def test_current_project_env_override(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_PROJECT", "stage")
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.current_project()
        assert result["alias"] == "stage"
        assert result["source"] == "env"
        assert result["pinned"] == "prod"
        assert result["env_points_to_configured_project"] is True

    def test_current_project_env_unknown(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_PROJECT", "mystery")
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.current_project()
        assert result["alias"] == "mystery"
        assert result["env_points_to_configured_project"] is False

    def test_current_project_no_pin_no_env(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        # Empty store -- no pin possible
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)

        result = service.current_project()
        assert result["alias"] is None
        assert result["source"] == "none"

    # ── resolve_pinned_alias precedence ────────────────────────────────

    def test_resolve_explicit_wins(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_PROJECT", "stage")
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        alias, source = service.resolve_pinned_alias(explicit="prod")
        assert alias == "prod"
        assert source == "explicit"

    def test_resolve_env_beats_pin(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_PROJECT", "stage")
        store = self._seed_two(tmp_config_dir)
        # default is prod (first added)
        service = ProjectService(config_store=store)

        alias, source = service.resolve_pinned_alias()
        assert alias == "stage"
        assert source == "env"

    def test_resolve_pin_used(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)

        alias, source = service.resolve_pinned_alias()
        assert alias == "prod"
        assert source == "pin"

    def test_resolve_sole_project_fallback(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "only",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="t",
                project_name="Only",
                project_id=7,
            ),
        )
        # Clear the pin to exercise the sole-project fallback.
        cfg = store.load()
        cfg.default_project = ""
        store.save(cfg)

        service = ProjectService(config_store=store)
        alias, source = service.resolve_pinned_alias()
        assert alias == "only"
        assert source == "sole"

    def test_resolve_fail_hard_multi_no_pin(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = self._seed_two(tmp_config_dir)
        cfg = store.load()
        cfg.default_project = ""
        store.save(cfg)

        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="Multiple projects"):
            service.resolve_pinned_alias()

    def test_resolve_explicit_unknown_raises(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="not found"):
            service.resolve_pinned_alias(explicit="ghost")

    def test_resolve_env_unknown_raises(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.setenv("KBAGENT_PROJECT", "mystery")
        store = self._seed_two(tmp_config_dir)
        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="not registered"):
            service.resolve_pinned_alias()

    def test_resolve_no_projects_raises(self, tmp_config_dir: Path, monkeypatch) -> None:
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = ConfigStore(config_dir=tmp_config_dir)
        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="No projects configured"):
            service.resolve_pinned_alias()

    def test_resolve_pinned_alias_points_to_unregistered(
        self, tmp_config_dir: Path, monkeypatch
    ) -> None:
        """Stale pin (pointing at deleted project) raises a repair-friendly ConfigError."""
        monkeypatch.delenv("KBAGENT_PROJECT", raising=False)
        store = self._seed_two(tmp_config_dir)
        # Hand-edit default_project to a value that isn't in projects.
        cfg = store.load()
        cfg.default_project = "ghost"
        store.save(cfg)

        service = ProjectService(config_store=store)
        with pytest.raises(ConfigError, match="not registered"):
            service.resolve_pinned_alias()


class TestConfigServiceListConfigs:
    """Tests for ConfigService.list_configs()."""

    def test_list_configs_single_project_all_configs(self, tmp_config_dir: Path) -> None:
        """list_configs returns all configs from a single project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                project_name="Production",
                project_id=1234,
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                project_name="Production",
                project_id=1234,
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-abcdef-ghijklmnopqrst",
                project_name="Development",
                project_id=5678,
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        mock_client.list_components.assert_called_once_with(
            component_type="extractor", branch_id=None
        )

    def test_list_configs_filter_by_component_id(self, tmp_config_dir: Path) -> None:
        """list_configs filters configs to only the specified component_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

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
        store.add_project(
            "good",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-good-abcdefghijklmnop",
            ),
        )
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-bad-abcdefghijklmnopq",
            ),
        )

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
        store.add_project(
            "empty",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        mock_client.list_components.assert_called_once_with(
            component_type="extractor", branch_id=None
        )

    def test_list_configs_multiple_aliases(self, tmp_config_dir: Path) -> None:
        """list_configs with multiple aliases queries exactly those projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "proj-a",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-aaa-abcdefghijklmnop",
            ),
        )
        store.add_project(
            "proj-b",
            ProjectConfig(
                stack_url="https://b.com",
                token="902-bbb-abcdefghijklmnop",
            ),
        )
        store.add_project(
            "proj-c",
            ProjectConfig(
                stack_url="https://c.com",
                token="903-ccc-abcdefghijklmnop",
            ),
        )

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

    def test_list_configs_with_branch_id(self, tmp_config_dir: Path) -> None:
        """list_configs passes explicit branch_id to client.list_components."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs(branch_id=42)
        configs = result["configs"]

        assert len(configs) == 3
        mock_client.list_components.assert_called_once_with(component_type=None, branch_id=42)

    def test_list_configs_uses_active_branch(self, tmp_config_dir: Path) -> None:
        """list_configs uses project.active_branch_id when no explicit branch_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                active_branch_id=99,
            ),
        )

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        configs = result["configs"]

        assert len(configs) == 3
        mock_client.list_components.assert_called_once_with(component_type=None, branch_id=99)


class TestConfigServiceGetConfigDetail:
    """Tests for ConfigService.get_config_detail()."""

    def test_get_config_detail_success(self, tmp_config_dir: Path) -> None:
        """get_config_detail returns full config detail with project_alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
            "keboola.ex-db-snowflake", "101", branch_id=None
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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

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

    def test_get_config_detail_with_branch_id(self, tmp_config_dir: Path) -> None:
        """get_config_detail passes branch_id to client and includes it in result."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        detail_response = {
            "id": "101",
            "name": "Branch Config",
            "description": "Config on a dev branch",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "branch_db"}},
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
            branch_id=55,
        )

        assert result["id"] == "101"
        assert result["name"] == "Branch Config"
        assert result["project_alias"] == "prod"
        assert result["branch_id"] == 55
        mock_client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "101", branch_id=55
        )
        mock_client.close.assert_called_once()

    def test_get_config_detail_uses_active_branch(self, tmp_config_dir: Path) -> None:
        """get_config_detail uses project.active_branch_id when no explicit branch_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                active_branch_id=77,
            ),
        )

        detail_response = {
            "id": "101",
            "name": "Active Branch Config",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {},
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

        assert result["branch_id"] == 77
        mock_client.get_config_detail.assert_called_once_with(
            "keboola.ex-db-snowflake", "101", branch_id=77
        )
        mock_client.close.assert_called_once()

    def test_get_config_detail_with_state_single_mode(self, tmp_config_dir: Path) -> None:
        """Single-config --with-state triggers get_config_state and overrides state."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        # Detail endpoint returns stale state; get_config_state returns fresh state.
        detail_response = {
            "id": "101",
            "name": "Production Load",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {}},
            "rows": [],
            "state": {"stale": True},
        }
        fresh_state = {"last_cursor": "2026-04-23T10:00:00Z", "fresh": True}

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response
        mock_client.get_config_state.return_value = fresh_state

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id="101",
            with_state=True,
        )

        assert result["state"] == fresh_state
        mock_client.get_config_state.assert_called_once_with(
            "keboola.ex-db-snowflake", "101", branch_id=None
        )
        mock_client.close.assert_called_once()

    def test_get_config_detail_bulk_single_project(self, tmp_config_dir: Path) -> None:
        """Bulk mode (no config_id) returns {configs, errors} for a single project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        components_response = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake Extractor",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "101",
                        "name": "Prod",
                        "description": "",
                        "configuration": {"parameters": {"host": "a"}},
                        "rows": [],
                        "version": 1,
                        "isDisabled": False,
                        "isDeleted": False,
                    },
                    {
                        "id": "102",
                        "name": "Dev",
                        "description": "",
                        "configuration": {"parameters": {"host": "b"}},
                        "rows": [],
                        "version": 1,
                        "isDisabled": False,
                        "isDeleted": False,
                    },
                ],
            },
            {
                "id": "keboola.wr-db-snowflake",
                "name": "Snowflake Writer",
                "type": "writer",
                "configurations": [
                    {"id": "999", "name": "X", "configuration": {}, "rows": []},
                ],
            },
        ]

        mock_client = MagicMock()
        mock_client.list_components_with_configs.return_value = components_response

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id=None,
        )

        assert "configs" in result
        assert "errors" in result
        # Only configs of the requested component_id survive the filter
        assert [c["config_id"] for c in result["configs"]] == ["101", "102"]
        assert all(c["project_alias"] == "prod" for c in result["configs"])
        assert all(c["component_id"] == "keboola.ex-db-snowflake" for c in result["configs"])
        # Bulk call used list_components_with_configs, not per-config detail
        mock_client.list_components_with_configs.assert_called_once()
        mock_client.get_config_detail.assert_not_called()
        mock_client.close.assert_called_once()

    def test_get_config_detail_bulk_multi_project(self, tmp_config_dir: Path) -> None:
        """Bulk mode across projects aggregates results and tags each row with alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "stage",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

        prod_components = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake",
                "type": "extractor",
                "configurations": [
                    {"id": "101", "name": "ProdCfg", "configuration": {}, "rows": []},
                ],
            },
        ]
        stage_components = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake",
                "type": "extractor",
                "configurations": [
                    {"id": "201", "name": "StageCfg", "configuration": {}, "rows": []},
                ],
            },
        ]

        def factory(url, token):
            mc = MagicMock()
            if "901" in token:
                mc.list_components_with_configs.return_value = prod_components
            else:
                mc.list_components_with_configs.return_value = stage_components
            return mc

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id=None,
            aliases=["prod", "stage"],
        )

        aliases_seen = {c["project_alias"] for c in result["configs"]}
        assert aliases_seen == {"prod", "stage"}
        assert len(result["configs"]) == 2
        assert len(result["errors"]) == 0

    def test_get_config_detail_bulk_partial_failure(self, tmp_config_dir: Path) -> None:
        """Bulk mode: one project fails, others succeed; error captured in errors list."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="999-broken-token-value-here-1234",
            ),
        )

        prod_components = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake",
                "type": "extractor",
                "configurations": [
                    {"id": "101", "name": "OK", "configuration": {}, "rows": []},
                ],
            },
        ]

        def factory(url, token):
            mc = MagicMock()
            if "901" in token:
                mc.list_components_with_configs.return_value = prod_components
            else:
                mc.list_components_with_configs.side_effect = KeboolaApiError(
                    message="Invalid token",
                    status_code=401,
                    error_code="INVALID_TOKEN",
                    retryable=False,
                )
            return mc

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id=None,
            aliases=["prod", "broken"],
        )

        assert len(result["configs"]) == 1
        assert result["configs"][0]["project_alias"] == "prod"
        assert len(result["errors"]) == 1
        assert result["errors"][0]["project_alias"] == "broken"
        assert result["errors"][0]["error_code"] == "INVALID_TOKEN"

    def test_get_config_detail_bulk_with_state(self, tmp_config_dir: Path) -> None:
        """Bulk --with-state uses include_state on the listing call (no N+1)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        components_response = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "101",
                        "name": "cfg1",
                        "configuration": {},
                        "rows": [],
                        "state": {"cursor": "abc"},
                    },
                    {
                        "id": "102",
                        "name": "cfg2",
                        "configuration": {},
                        "rows": [],
                        "state": {},
                    },
                ],
            },
        ]

        mock_client = MagicMock()
        mock_client.list_components_with_configs.return_value = components_response

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id=None,
            with_state=True,
        )

        assert result["configs"][0]["state"] == {"cursor": "abc"}
        assert result["configs"][1]["state"] == {}
        # The single call carries include_state=True -- no N+1
        mock_client.list_components_with_configs.assert_called_once_with(
            branch_id=None,
            include_state=True,
        )
        mock_client.get_config_state.assert_not_called()

    def test_get_config_detail_bulk_no_configs_for_component(self, tmp_config_dir: Path) -> None:
        """Bulk mode: component has no configs -> empty configs list, no error."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = MagicMock()
        # Other components exist but none matching our component_id
        mock_client.list_components_with_configs.return_value = [
            {
                "id": "keboola.wr-db-snowflake",
                "name": "Writer",
                "type": "writer",
                "configurations": [{"id": "999", "name": "x", "configuration": {}, "rows": []}],
            },
        ]

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_config_detail(
            alias="prod",
            component_id="keboola.ex-db-snowflake",
            config_id=None,
        )

        assert result["configs"] == []
        assert result["errors"] == []

    def test_get_config_detail_bulk_rejects_multi_project_with_branch(
        self, tmp_config_dir: Path
    ) -> None:
        """Bulk mode with --branch requires exactly one --project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        for alias in ("prod", "stage"):
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=f"{alias}-token-value-XYZ-1234567890",
                ),
            )
        service = ConfigService(config_store=store)

        with pytest.raises(ConfigError, match="exactly one --project"):
            service.get_config_detail(
                alias="prod",
                component_id="keboola.ex-db-snowflake",
                config_id=None,
                branch_id=42,
                aliases=["prod", "stage"],
            )


class TestConfigServiceListConfigsIncludeRows:
    """Tests for ConfigService.list_configs(include_rows=...)."""

    def test_list_configs_include_rows_switches_to_list_components_with_configs(
        self, tmp_config_dir: Path
    ) -> None:
        """With include_rows=True, list_configs calls list_components_with_configs."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        components_response = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake",
                "type": "extractor",
                "configurations": [
                    {
                        "id": "101",
                        "name": "Prod",
                        "description": "",
                        "configuration": {"parameters": {"host": "a"}},
                        "rows": [{"id": "r1", "configuration": {"x": 1}}],
                    },
                ],
            },
        ]

        mock_client = MagicMock()
        mock_client.list_components_with_configs.return_value = components_response

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs(include_rows=True)
        configs = result["configs"]

        assert len(configs) == 1
        entry = configs[0]
        # Full body is attached (this is the point of include_rows)
        assert entry["configuration"] == {"parameters": {"host": "a"}}
        assert entry["rows"] == [{"id": "r1", "configuration": {"x": 1}}]
        # Ordinary summary fields still present
        assert entry["config_id"] == "101"
        assert entry["config_name"] == "Prod"

        # list_components_with_configs was called, not list_components
        mock_client.list_components_with_configs.assert_called_once()
        mock_client.list_components.assert_not_called()

    def test_list_configs_without_include_rows_uses_list_components(
        self, tmp_config_dir: Path
    ) -> None:
        """Default (include_rows=False) preserves the original lightweight listing."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        configs = result["configs"]

        assert len(configs) >= 1
        # Default shape: no configuration / rows keys
        assert "configuration" not in configs[0]
        assert "rows" not in configs[0]
        mock_client.list_components.assert_called_once()
        mock_client.list_components_with_configs.assert_not_called()


class TestConfigServiceSearchConfigs:
    """Tests for ConfigService.search_configs() with branch_id support."""

    def test_search_configs_with_branch_id(self, tmp_config_dir: Path) -> None:
        """search_configs passes branch_id to client.list_components_with_configs."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        # search_configs uses list_components_with_configs (include=configuration,rows)
        # so row-level properties are part of the search tree (see #196).
        mock_client = MagicMock()
        mock_client.list_components_with_configs.return_value = SAMPLE_COMPONENTS
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.search_configs(query="Production", branch_id=123)
        matches = result["matches"]

        # "Production Load" config name matches the query
        assert len(matches) == 1
        assert matches[0]["config_name"] == "Production Load"

        mock_client.list_components_with_configs.assert_called_once_with(
            branch_id=123, component_type=None
        )
        mock_client.close.assert_called_once()

    def test_search_configs_uses_active_branch(self, tmp_config_dir: Path) -> None:
        """search_configs uses project.active_branch_id when no explicit branch_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                active_branch_id=88,
            ),
        )

        mock_client = MagicMock()
        mock_client.list_components_with_configs.return_value = SAMPLE_COMPONENTS
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.search_configs(query="nonexistent-query")

        mock_client.list_components_with_configs.assert_called_once_with(
            branch_id=88, component_type=None
        )
        mock_client.close.assert_called_once()


class TestResolveProjects:
    """Tests for ConfigService.resolve_projects()."""

    def test_resolve_all_projects(self, tmp_config_dir: Path) -> None:
        """resolve_projects with no aliases returns all projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://b.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

        service = ConfigService(config_store=store)
        result = service.resolve_projects()
        assert set(result.keys()) == {"prod", "dev"}

    def test_resolve_specific_aliases(self, tmp_config_dir: Path) -> None:
        """resolve_projects with aliases returns only matching projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://b.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

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
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://a.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        service = ConfigService(config_store=store)
        result = service.resolve_projects(aliases=[])
        assert set(result.keys()) == {"prod"}


# ---------------------------------------------------------------------------
# Helpers for JobService tests
# ---------------------------------------------------------------------------

SAMPLE_JOBS = [
    {
        "id": 1001,
        "status": "success",
        "component": "keboola.ex-db-snowflake",
        "configId": "101",
        "createdTime": "2026-02-26T10:00:00Z",
        "durationSeconds": 45,
    },
    {
        "id": 1002,
        "status": "error",
        "component": "keboola.wr-db-snowflake",
        "configId": "201",
        "createdTime": "2026-02-26T11:00:00Z",
        "durationSeconds": 120,
    },
]

SAMPLE_JOBS_2 = [
    {
        "id": 2001,
        "status": "processing",
        "component": "keboola.python-transformation-v2",
        "configId": "301",
        "createdTime": "2026-02-26T12:00:00Z",
    },
]


def _make_list_jobs_client(jobs: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_jobs returning given data."""
    mock_client = MagicMock()
    mock_client.list_jobs.return_value = jobs
    return mock_client


class TestJobServiceListJobs:
    """Tests for JobService.list_jobs()."""

    def test_list_jobs_single_project(self, tmp_config_dir: Path) -> None:
        """list_jobs returns all jobs from a single project."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
                project_name="Production",
                project_id=1234,
            ),
        )

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_jobs()
        jobs = result["jobs"]
        errors = result["errors"]

        assert len(errors) == 0
        assert len(jobs) == 2
        assert jobs[0]["project_alias"] == "prod"
        assert jobs[0]["id"] == 1001
        assert jobs[1]["status"] == "error"

    def test_list_jobs_multi_project_aggregation(self, tmp_config_dir: Path) -> None:
        """list_jobs aggregates jobs across multiple projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

        prod_client = _make_list_jobs_client(SAMPLE_JOBS)
        dev_client = _make_list_jobs_client(SAMPLE_JOBS_2)

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        service = JobService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_jobs()
        jobs = result["jobs"]

        assert len(jobs) == 3  # 2 from prod + 1 from dev
        prod_jobs = [j for j in jobs if j["project_alias"] == "prod"]
        dev_jobs = [j for j in jobs if j["project_alias"] == "dev"]
        assert len(prod_jobs) == 2
        assert len(dev_jobs) == 1

    def test_list_jobs_partial_failure(self, tmp_config_dir: Path) -> None:
        """list_jobs continues when one project fails, reporting the error."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "good",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-good-abcdefghijklmnop",
            ),
        )
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-bad-abcdefghijklmnopq",
            ),
        )

        good_client = _make_list_jobs_client(SAMPLE_JOBS)
        bad_client = MagicMock()
        bad_client.list_jobs.side_effect = KeboolaApiError(
            message="Token expired for bad project",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        service = JobService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_jobs()
        jobs = result["jobs"]
        errors = result["errors"]

        assert len(jobs) == 2
        assert all(j["project_alias"] == "good" for j in jobs)
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "bad"
        assert errors[0]["error_code"] == "INVALID_TOKEN"

    def test_list_jobs_with_filters(self, tmp_config_dir: Path) -> None:
        """list_jobs passes filters to the client."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = _make_list_jobs_client([])
        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_jobs(
            component_id="keboola.ex-db-snowflake",
            config_id="42",
            status="error",
            limit=10,
        )

        mock_client.list_jobs.assert_called_once_with(
            component_id="keboola.ex-db-snowflake",
            config_id="42",
            status="error",
            limit=10,
        )

    def test_list_jobs_unknown_alias_raises_config_error(self, tmp_config_dir: Path) -> None:
        """list_jobs with unknown alias raises ConfigError."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = JobService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.list_jobs(aliases=["nonexistent"])

    def test_list_jobs_empty_results(self, tmp_config_dir: Path) -> None:
        """list_jobs returns empty jobs list when no jobs exist."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "empty",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = _make_list_jobs_client([])
        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_jobs()
        assert result["jobs"] == []
        assert result["errors"] == []

    def test_list_jobs_no_projects_configured(self, tmp_config_dir: Path) -> None:
        """list_jobs with no projects returns empty results."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = JobService(config_store=store)

        result = service.list_jobs()
        assert result["jobs"] == []
        assert result["errors"] == []

    def test_list_jobs_client_closed_after_use(self, tmp_config_dir: Path) -> None:
        """list_jobs always closes the client after querying."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_jobs()
        mock_client.close.assert_called_once()

    def test_list_jobs_client_closed_on_error(self, tmp_config_dir: Path) -> None:
        """list_jobs closes the client even when the API call fails."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = MagicMock()
        mock_client.list_jobs.side_effect = KeboolaApiError(
            message="Server error",
            status_code=500,
            error_code="API_ERROR",
            retryable=True,
        )
        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        service.list_jobs()
        mock_client.close.assert_called_once()

    def test_list_jobs_project_filter(self, tmp_config_dir: Path) -> None:
        """list_jobs with aliases only queries specified projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.north-europe.azure.keboola.com",
                token="532-abcdef-ghijklmnopqrst",
            ),
        )

        prod_client = _make_list_jobs_client(SAMPLE_JOBS)
        dev_client = _make_list_jobs_client(SAMPLE_JOBS_2)

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        service = JobService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_jobs(aliases=["prod"])
        jobs = result["jobs"]

        assert len(jobs) == 2
        assert all(j["project_alias"] == "prod" for j in jobs)
        dev_client.list_jobs.assert_not_called()


class TestJobServiceGetJobDetail:
    """Tests for JobService.get_job_detail()."""

    def test_get_job_detail_success(self, tmp_config_dir: Path) -> None:
        """get_job_detail returns full job detail with project_alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        detail_response = {
            "id": "1001",
            "status": "success",
            "component": "keboola.ex-db-snowflake",
            "config": "101",
            "result": {"message": "Job completed"},
        }

        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = detail_response

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_job_detail(alias="prod", job_id="1001")

        assert result["id"] == "1001"
        assert result["status"] == "success"
        assert result["project_alias"] == "prod"
        mock_client.get_job_detail.assert_called_once_with("1001")
        mock_client.close.assert_called_once()

    def test_get_job_detail_unknown_alias(self, tmp_config_dir: Path) -> None:
        """get_job_detail raises ConfigError for unknown alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        service = JobService(config_store=store)

        with pytest.raises(ConfigError, match="not found"):
            service.get_job_detail(alias="nonexistent", job_id="1001")

    def test_get_job_detail_api_error(self, tmp_config_dir: Path) -> None:
        """get_job_detail propagates KeboolaApiError from the client."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = MagicMock()
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="Job not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError) as exc_info:
            service.get_job_detail(alias="prod", job_id="999999")

        assert exc_info.value.error_code == "NOT_FOUND"
        mock_client.close.assert_called_once()

    def test_get_job_detail_client_closed_on_error(self, tmp_config_dir: Path) -> None:
        """get_job_detail closes the client even when API call fails."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-10493007-VDtlEDWDF6Tx5V8jjE8FshFlqM0Hl0c08KHqpt0k",
            ),
        )

        mock_client = MagicMock()
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="Server error",
            status_code=500,
            error_code="API_ERROR",
            retryable=True,
        )

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        with pytest.raises(KeboolaApiError):
            service.get_job_detail("prod", "1001")

        mock_client.close.assert_called_once()


class TestJobServiceRunJob:
    """Tests for JobService.run_job() - including branch_id support."""

    def test_run_job_without_branch(self, tmp_config_dir: Path) -> None:
        """run_job without branch_id calls create_job without branch_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abc-defghijklmnopqrst",
                project_name="Production",
                project_id=1234,
            ),
        )

        mock_client = MagicMock()
        mock_client.create_job.return_value = {
            "id": 555,
            "status": "waiting",
            "component": "keboola.ex-http",
        }
        # Parent config has no linked variables -- resolver returns None
        mock_client.get_config_detail.return_value = {}

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.run_job(
            alias="prod",
            component_id="keboola.ex-http",
            config_id="42",
        )

        assert result["id"] == 555
        assert result["project_alias"] == "prod"
        mock_client.create_job.assert_called_once_with(
            component_id="keboola.ex-http",
            config_id="42",
            config_row_ids=None,
            branch_id=None,
            variable_values_id=None,
        )
        mock_client.close.assert_called_once()

    def test_run_job_with_branch(self, tmp_config_dir: Path) -> None:
        """run_job with branch_id forwards it to create_job."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abc-defghijklmnopqrst",
                project_name="Dev",
                project_id=5678,
            ),
        )

        mock_client = MagicMock()
        mock_client.create_job.return_value = {
            "id": 556,
            "status": "waiting",
            "branchId": "789",
        }
        mock_client.get_config_detail.return_value = {}

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.run_job(
            alias="dev",
            component_id="keboola.snowflake-transformation",
            config_id="100",
            branch_id=789,
        )

        assert result["id"] == 556
        assert result["project_alias"] == "dev"
        mock_client.create_job.assert_called_once_with(
            component_id="keboola.snowflake-transformation",
            config_id="100",
            config_row_ids=None,
            branch_id=789,
            variable_values_id=None,
        )

    def test_run_job_with_branch_and_wait(self, tmp_config_dir: Path) -> None:
        """run_job with branch_id and wait=True polls until completion."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "dev",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abc-defghijklmnopqrst",
                project_name="Dev",
                project_id=5678,
            ),
        )

        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 557, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 557,
            "status": "success",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.run_job(
            alias="dev",
            component_id="keboola.ex-http",
            config_id="42",
            branch_id=123,
            wait=True,
            timeout=60.0,
        )

        assert result["status"] == "success"
        assert result["project_alias"] == "dev"
        mock_client.create_job.assert_called_once_with(
            component_id="keboola.ex-http",
            config_id="42",
            config_row_ids=None,
            branch_id=123,
            variable_values_id=None,
        )
        mock_client.wait_for_queue_job.assert_called_once_with(
            "557", max_wait=60.0, poll_strategy="exponential"
        )


class TestJobServiceQueuePollingParity:
    """PR4: log-tail capture + auto-cancel on --timeout + poll-strategy plumbing."""

    def _store(self, tmp_config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abc-defghijklmnopqrst",
                project_name="Prod",
                project_id=1234,
            ),
        )
        return store

    def _service(self, store: ConfigStore, mock_client: MagicMock) -> JobService:
        return JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

    def test_run_job_threads_poll_strategy_to_client(self, tmp_config_dir: Path) -> None:
        """poll_strategy kwarg reaches wait_for_queue_job unchanged."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 700, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 700,
            "status": "success",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}

        service = self._service(self._store(tmp_config_dir), mock_client)
        service.run_job(
            alias="prod",
            component_id="keboola.ex-http",
            config_id="42",
            wait=True,
            timeout=60.0,
            poll_strategy="fixed",
            no_variables=True,
        )

        mock_client.wait_for_queue_job.assert_called_once_with(
            "700", max_wait=60.0, poll_strategy="fixed"
        )

    def test_run_job_rejects_unknown_strategy(self, tmp_config_dir: Path) -> None:
        """Bad poll_strategy fails at service boundary, not at client."""
        service = self._service(self._store(tmp_config_dir), MagicMock())
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                poll_strategy="linear",
                no_variables=True,
            )
        assert exc_info.value.error_code == "INVALID_ARGUMENT"

    def test_run_job_rejects_negative_log_tail(self, tmp_config_dir: Path) -> None:
        service = self._service(self._store(tmp_config_dir), MagicMock())
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                log_tail_lines=-1,
                no_variables=True,
            )
        assert exc_info.value.error_code == "INVALID_ARGUMENT"

    def test_run_job_success_no_tail_attached(self, tmp_config_dir: Path) -> None:
        """status=success does NOT fetch events (log tail only for non-success)."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 701, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 701,
            "status": "success",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}

        service = self._service(self._store(tmp_config_dir), mock_client)
        result = service.run_job(
            alias="prod",
            component_id="keboola.ex-http",
            config_id="42",
            wait=True,
            no_variables=True,
        )
        assert "logTail" not in result
        mock_client.fetch_job_events.assert_not_called()

    def test_run_job_warning_attaches_log_tail(self, tmp_config_dir: Path) -> None:
        """status=warning surfaces a logTail of the first N events (newest-first)."""
        # Storage Events returns newest -> oldest; emulate that ordering so
        # the slice asserts the client didn't accidentally reverse it.
        events = [{"id": 249 - i, "message": f"event {249 - i}"} for i in range(250)]
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 702, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 702,
            "runId": "702-run",
            "status": "warning",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}
        mock_client.fetch_job_events.return_value = events

        service = self._service(self._store(tmp_config_dir), mock_client)
        result = service.run_job(
            alias="prod",
            component_id="keboola.ex-http",
            config_id="42",
            wait=True,
            log_tail_lines=100,
            no_variables=True,
        )
        # Storage Events yields newest-first; we take [:100] so the head
        # stays newest. IDs 249 -> 150.
        assert len(result["logTail"]) == 100
        assert result["logTail"][0]["id"] == 249
        assert result["logTail"][-1]["id"] == 150
        # runId (not raw id) must have been the query key.
        mock_client.fetch_job_events.assert_called_once_with("702-run", limit=100)

    def test_run_job_zero_tail_skips_fetch(self, tmp_config_dir: Path) -> None:
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 703, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 703,
            "status": "terminated",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}

        service = self._service(self._store(tmp_config_dir), mock_client)
        result = service.run_job(
            alias="prod",
            component_id="keboola.ex-http",
            config_id="42",
            wait=True,
            log_tail_lines=0,
            no_variables=True,
        )
        mock_client.fetch_job_events.assert_not_called()
        assert "logTail" not in result

    def test_run_job_queue_failure_attaches_tail_and_reraises(self, tmp_config_dir: Path) -> None:
        """QUEUE_JOB_FAILED re-raises with logTail tucked into exc.details."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 704, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 704 failed: SQL error",
            status_code=500,
            error_code="QUEUE_JOB_FAILED",
        )
        # The service fetches job detail on failure to resolve runId.
        mock_client.get_job_detail.return_value = {
            "id": "704",
            "runId": "704",
            "status": "error",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}
        mock_client.fetch_job_events.return_value = [
            {"uuid": "u1", "type": "error", "message": "SQL error"},
        ]

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                no_variables=True,
            )
        assert exc_info.value.error_code == "QUEUE_JOB_FAILED"
        assert exc_info.value.details["logTail"][0]["message"] == "SQL error"
        # kill_job must NOT be called when the job failed on its own.
        mock_client.kill_job.assert_not_called()
        # runId used as the query key, not the Queue job id.
        mock_client.fetch_job_events.assert_called_once_with("704", limit=200)

    def test_run_job_timeout_issues_kill_and_raises_terminated(self, tmp_config_dir: Path) -> None:
        """QUEUE_JOB_TIMEOUT -> kill_job + JOB_TIMEOUT_TERMINATED with job payload."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 705, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 705 did not complete within 5s",
            status_code=504,
            error_code="QUEUE_JOB_TIMEOUT",
        )
        mock_client.kill_job.return_value = {
            "id": 705,
            "status": "terminating",
            "desiredStatus": "terminating",
        }
        mock_client.get_job_detail.return_value = {
            "id": 705,
            "runId": "705-run",
            "status": "terminated",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}
        mock_client.fetch_job_events.return_value = [{"uuid": "u1", "message": "x"}]

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                timeout=5.0,
                no_variables=True,
            )
        assert exc_info.value.error_code == "JOB_TIMEOUT_TERMINATED"
        mock_client.kill_job.assert_called_once_with("705")
        details = exc_info.value.details
        assert details["job"]["status"] == "terminated"
        assert details["logTail"] == [{"uuid": "u1", "message": "x"}]
        # runId from the terminated job detail used as the lookup key.
        mock_client.fetch_job_events.assert_called_once_with("705-run", limit=200)

    def test_run_job_timeout_kill_fails_falls_back(self, tmp_config_dir: Path) -> None:
        """If kill_job AND the follow-up GET fail, surface QUEUE_JOB_TIMEOUT (retryable)."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 706, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 706 did not complete within 5s",
            status_code=504,
            error_code="QUEUE_JOB_TIMEOUT",
            retryable=True,
        )
        mock_client.kill_job.side_effect = KeboolaApiError(
            message="network down",
            status_code=0,
            error_code="CONNECTION_ERROR",
        )
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="still down",
            status_code=0,
            error_code="CONNECTION_ERROR",
        )
        mock_client.get_config_detail.return_value = {}
        mock_client.fetch_job_events.return_value = []

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                timeout=5.0,
                no_variables=True,
            )
        assert exc_info.value.error_code == "QUEUE_JOB_TIMEOUT"
        assert exc_info.value.retryable is True

    def test_run_job_log_tail_fetch_failure_is_swallowed(self, tmp_config_dir: Path) -> None:
        """A failing fetch_job_events must not mask the original job failure."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 707, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="Queue job 707 failed: SQL error",
            status_code=500,
            error_code="QUEUE_JOB_FAILED",
        )
        mock_client.fetch_job_events.side_effect = KeboolaApiError(
            message="events 500",
            status_code=500,
            error_code="UNKNOWN_ERROR",
        )
        mock_client.get_config_detail.return_value = {}

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                no_variables=True,
            )
        # Original error preserved; details has no logTail key when fetch fails.
        assert exc_info.value.error_code == "QUEUE_JOB_FAILED"
        assert "logTail" not in exc_info.value.details

    def test_run_job_unhandled_wait_code_bubbles_up_unchanged(self, tmp_config_dir: Path) -> None:
        """A wait error that is neither QUEUE_JOB_FAILED nor QUEUE_JOB_TIMEOUT
        must re-raise the original instance with no mutation and no kill attempt.
        Locks the observability fall-through path added in the review loop."""
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 708, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = KeboolaApiError(
            message="token rotated mid-run",
            status_code=401,
            error_code="INVALID_TOKEN",
        )
        mock_client.get_config_detail.return_value = {}

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                no_variables=True,
            )
        assert exc_info.value.error_code == "INVALID_TOKEN"
        # No tail fetch, no kill -- this path is for errors we don't specialise.
        mock_client.fetch_job_events.assert_not_called()
        mock_client.kill_job.assert_not_called()
        # exc passes through without a logTail (mutation would be a bug).
        assert "logTail" not in exc_info.value.details

    def test_run_job_failure_exception_chaining_does_not_mutate_original(
        self, tmp_config_dir: Path
    ) -> None:
        """QUEUE_JOB_FAILED produces a NEW exception chained from the original.

        Guarantees we do not mutate a caught exception's .details dict
        (which would contaminate any shared instance / retry harness).
        """
        original_details: dict = {}
        original = KeboolaApiError(
            message="Queue job 709 failed: SQL error",
            status_code=500,
            error_code="QUEUE_JOB_FAILED",
            details=original_details,
        )
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 709, "status": "waiting"}
        mock_client.wait_for_queue_job.side_effect = original
        mock_client.get_job_detail.return_value = {
            "id": "709",
            "runId": "709",
            "status": "error",
            "isFinished": True,
        }
        mock_client.get_config_detail.return_value = {}
        mock_client.fetch_job_events.return_value = [{"uuid": "u1", "message": "err"}]

        service = self._service(self._store(tmp_config_dir), mock_client)
        with pytest.raises(KeboolaApiError) as exc_info:
            service.run_job(
                alias="prod",
                component_id="keboola.ex-http",
                config_id="42",
                wait=True,
                no_variables=True,
            )
        raised = exc_info.value
        # Raised instance is NEW, not the one side_effect handed us.
        assert raised is not original
        # Chain is explicit (raise ... from original).
        assert raised.__cause__ is original
        # Original details dict was NOT mutated.
        assert original_details == {}
        assert "logTail" not in original.details
        # But the new exception carries the tail.
        assert raised.details["logTail"][0]["uuid"] == "u1"


class TestSafeFetchLogTailDefensiveSort:
    """Ensure _safe_fetch_log_tail enforces newest-first ordering regardless of
    what the API returned (PR4 review round 1 finding)."""

    def test_sorts_events_by_created_desc(self) -> None:
        """API returns events in arbitrary order -> we still emit newest first."""
        from keboola_agent_cli.services.job_service import _safe_fetch_log_tail

        mock_client = MagicMock()
        # Deliberately shuffled order from the "API"
        mock_client.fetch_job_events.return_value = [
            {"uuid": "a", "created": "2026-04-22T09:54:27+0200", "message": "middle"},
            {"uuid": "b", "created": "2026-04-22T09:54:30+0200", "message": "newest"},
            {"uuid": "c", "created": "2026-04-22T09:54:10+0200", "message": "oldest"},
        ]
        tail = _safe_fetch_log_tail(mock_client, {"id": "x", "runId": "x"}, limit=10)
        assert [e["uuid"] for e in tail] == ["b", "a", "c"]

    def test_missing_created_sorts_last(self) -> None:
        """Events without `created` should not jump to the top of the tail."""
        from keboola_agent_cli.services.job_service import _safe_fetch_log_tail

        mock_client = MagicMock()
        mock_client.fetch_job_events.return_value = [
            {"uuid": "no_created", "message": "missing"},
            {"uuid": "timestamped", "created": "2026-04-22T09:54:30+0200", "message": "ok"},
        ]
        tail = _safe_fetch_log_tail(mock_client, {"id": "x", "runId": "x"}, limit=10)
        assert tail[0]["uuid"] == "timestamped"
        assert tail[1]["uuid"] == "no_created"


class TestJobServiceVariableValuesResolution:
    """Tests for `resolve_variable_values_id` + auto-resolution in `run_job`.

    Locks the contract: transformations with linked variables must run
    against the deployed values row, not empty strings.
    """

    def _store(self, tmp_config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-abc-defghijklmnopqrst",
                project_name="Production",
                project_id=1234,
            ),
        )
        return store

    def _service(self, store: ConfigStore, mock_client: MagicMock) -> JobService:
        return JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

    def test_resolve_uses_explicit_values_id_when_set(self) -> None:
        """configuration.variables_values_id wins over first-row fallback."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {
                "variables_id": "vars-cfg-42",
                "variables_values_id": "row-explicit",
            }
        }

        result = JobService.resolve_variable_values_id(
            client=mock_client,
            component_id="keboola.snowflake-transformation",
            config_id="100",
        )

        assert result == "row-explicit"
        mock_client.list_config_rows.assert_not_called()

    def test_resolve_falls_back_to_first_row(self) -> None:
        """When configuration.variables_id is set but values_id absent, use first row."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = [
            {"id": "row-first"},
            {"id": "row-second"},
        ]

        result = JobService.resolve_variable_values_id(
            client=mock_client,
            component_id="keboola.snowflake-transformation",
            config_id="100",
            branch_id=789,
        )

        assert result == "row-first"
        mock_client.list_config_rows.assert_called_once_with(
            component_id="keboola.variables",
            config_id="vars-cfg-42",
            branch_id=789,
        )

    def test_resolve_returns_none_when_no_variables_link(self) -> None:
        """Config without configuration.variables_id → None (skip variableValuesId)."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {"configuration": {}}

        result = JobService.resolve_variable_values_id(
            client=mock_client,
            component_id="keboola.ex-http",
            config_id="42",
        )

        assert result is None
        mock_client.list_config_rows.assert_not_called()

    def test_resolve_raises_when_variables_has_zero_rows(self) -> None:
        """Linked variables config with no rows → NO_VARIABLE_ROWS (fail fast)."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = []

        with pytest.raises(KeboolaApiError) as excinfo:
            JobService.resolve_variable_values_id(
                client=mock_client,
                component_id="keboola.snowflake-transformation",
                config_id="100",
            )
        assert excinfo.value.error_code == "NO_VARIABLE_ROWS"

    def test_resolve_raises_when_first_row_has_no_id(self) -> None:
        """Malformed first row (no ``id`` field) -> MALFORMED_VARIABLES_ROW.

        Locks the fail-loud contract: if the Storage API ever returns a row
        without a usable ``id``, the resolver must refuse rather than
        returning ``""`` and letting the Queue body quietly omit
        ``variableValuesId``.
        """
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = [{"name": "default"}]

        with pytest.raises(KeboolaApiError) as excinfo:
            JobService.resolve_variable_values_id(
                client=mock_client,
                component_id="keboola.snowflake-transformation",
                config_id="100",
            )
        assert excinfo.value.error_code == "MALFORMED_VARIABLES_ROW"

    def test_resolve_raises_when_first_row_id_is_empty_string(self) -> None:
        """Row with ``id=""`` also triggers MALFORMED_VARIABLES_ROW."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = [{"id": "", "name": "default"}]

        with pytest.raises(KeboolaApiError) as excinfo:
            JobService.resolve_variable_values_id(
                client=mock_client,
                component_id="keboola.snowflake-transformation",
                config_id="100",
            )
        assert excinfo.value.error_code == "MALFORMED_VARIABLES_ROW"

    def test_run_job_auto_resolves_values_id(self, tmp_config_dir: Path) -> None:
        """run_job dispatches resolver output to create_job's variable_values_id."""
        store = self._store(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = [{"id": "row-first"}]
        mock_client.create_job.return_value = {"id": 700, "status": "waiting"}

        result = self._service(store, mock_client).run_job(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="100",
        )

        assert result["resolvedVariableValuesId"] == "row-first"
        mock_client.create_job.assert_called_once_with(
            component_id="keboola.snowflake-transformation",
            config_id="100",
            config_row_ids=None,
            branch_id=None,
            variable_values_id="row-first",
        )

    def test_run_job_explicit_override_wins(self, tmp_config_dir: Path) -> None:
        """User-supplied --variable-values-id bypasses resolution entirely."""
        store = self._store(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 701, "status": "waiting"}

        self._service(store, mock_client).run_job(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="100",
            variable_values_id="row-user-picked",
        )

        # Resolver short-circuited; no config detail fetch.
        mock_client.get_config_detail.assert_not_called()
        mock_client.list_config_rows.assert_not_called()
        assert mock_client.create_job.call_args.kwargs["variable_values_id"] == "row-user-picked"

    def test_run_job_no_variables_skips_resolution(self, tmp_config_dir: Path) -> None:
        """--no-variables short-circuits the resolver (no detail fetch)."""
        store = self._store(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.create_job.return_value = {"id": 702, "status": "waiting"}

        result = self._service(store, mock_client).run_job(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="100",
            no_variables=True,
        )

        mock_client.get_config_detail.assert_not_called()
        assert mock_client.create_job.call_args.kwargs["variable_values_id"] is None
        assert "resolvedVariableValuesId" not in result

    def test_run_job_wait_preserves_resolved_variable_values_id(self, tmp_config_dir: Path) -> None:
        """resolvedVariableValuesId is stamped on the waited job, not the initial create result.

        Locks the ordering: `job = wait_for_queue_job(...)` replaces the dict
        returned by `create_job`; the stamp must happen AFTER the wait so the
        final returned dict carries it.
        """
        store = self._store(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-99"}
        }
        mock_client.list_config_rows.return_value = [{"id": "row-waited"}]
        mock_client.create_job.return_value = {"id": 750, "status": "waiting"}
        mock_client.wait_for_queue_job.return_value = {
            "id": 750,
            "status": "success",
            "isFinished": True,
        }

        result = self._service(store, mock_client).run_job(
            alias="prod",
            component_id="keboola.snowflake-transformation",
            config_id="100",
            wait=True,
            timeout=30.0,
        )

        assert result["status"] == "success"
        assert result["resolvedVariableValuesId"] == "row-waited"
        mock_client.wait_for_queue_job.assert_called_once_with(
            "750", max_wait=30.0, poll_strategy="exponential"
        )

    def test_run_job_closes_client_when_resolver_raises(self, tmp_config_dir: Path) -> None:
        """NO_VARIABLE_ROWS raised by the resolver inside run_job still closes the client.

        Locks the try/finally contract (best_practices.md §3): every error path
        that flows out of run_job -- including the resolver raising before
        create_job -- must release the HTTP client.
        """
        store = self._store(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = {
            "configuration": {"variables_id": "vars-cfg-42"}
        }
        mock_client.list_config_rows.return_value = []  # triggers NO_VARIABLE_ROWS

        with pytest.raises(KeboolaApiError) as excinfo:
            self._service(store, mock_client).run_job(
                alias="prod",
                component_id="keboola.snowflake-transformation",
                config_id="100",
            )

        assert excinfo.value.error_code == "NO_VARIABLE_ROWS"
        mock_client.create_job.assert_not_called()
        mock_client.close.assert_called_once()


def _make_job_store_and_project(tmp_config_dir: Path, alias: str = "prod") -> ConfigStore:
    """Helper to register a single project so JobService.resolve_projects() works."""
    store = ConfigStore(config_dir=tmp_config_dir)
    store.add_project(
        alias,
        ProjectConfig(
            stack_url="https://connection.keboola.com",
            token="901-abc-defghijklmnopqrst",
            project_name="Project",
            project_id=1234,
        ),
    )
    return store


class TestJobServiceTerminateJobs:
    """Tests for JobService.terminate_jobs() partition logic.

    Queue API returns four distinct HTTP shapes for kill; the service has to
    translate them into {killed, already_finished, not_found, failed}. The tests
    exercise each branch with a mocked client so we can assert the bucket
    assignment without hitting a live Queue.
    """

    def test_dry_run_reports_without_calling_kill(self, tmp_config_dir: Path) -> None:
        """dry_run=True short-circuits before any HTTP call and echoes the ids back."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["1", "2"], dry_run=True)

        assert result["dry_run"] is True
        assert result["would_terminate"] == ["1", "2"]
        assert result["killed"] == []
        mock_client.kill_job.assert_not_called()

    def test_kill_success_goes_to_killed_bucket(self, tmp_config_dir: Path) -> None:
        """HTTP 200 response flows into 'killed' with id/status/desiredStatus summary."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.kill_job.return_value = {
            "id": "111",
            "status": "processing",
            "desiredStatus": "terminating",
            "isFinished": False,
        }

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["111"])

        assert result["killed"] == [
            {"id": "111", "status": "processing", "desiredStatus": "terminating"}
        ]
        assert result["already_finished"] == []
        assert result["not_found"] == []
        assert result["failed"] == []

    def test_400_not_killable_goes_to_already_finished(self, tmp_config_dir: Path) -> None:
        """Job already in terminal state returns HTTP 400 -> already_finished (race-safe)."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.kill_job.side_effect = KeboolaApiError(
            message='Job id "1" is not in one of killable states (created,waiting,processing).',
            status_code=400,
            error_code="API_ERROR",
        )

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["1"])

        assert result["already_finished"] == [{"id": "1", "reason": "not_killable"}]
        assert result["killed"] == []
        assert result["failed"] == []

    def test_500_with_finished_job_goes_to_already_finished(self, tmp_config_dir: Path) -> None:
        """Queue API's 500/404 mismatch: GET confirms isFinished=True -> already_finished."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.kill_job.side_effect = KeboolaApiError(
            message="Internal Server Error occurred.",
            status_code=500,
            error_code="API_ERROR",
        )
        mock_client.get_job_detail.return_value = {
            "id": "2",
            "status": "success",
            "isFinished": True,
        }

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["2"])

        assert result["already_finished"] == [{"id": "2", "reason": "terminal_state"}]
        assert result["failed"] == []
        mock_client.get_job_detail.assert_called_once_with("2")

    def test_500_with_missing_job_goes_to_not_found(self, tmp_config_dir: Path) -> None:
        """Queue API's 500/404 mismatch: GET returns 404 -> not_found bucket."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.kill_job.side_effect = KeboolaApiError(
            message="Internal Server Error occurred.",
            status_code=500,
            error_code="API_ERROR",
        )
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="Not found",
            status_code=404,
            error_code="NOT_FOUND",
        )

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["99"])

        assert result["not_found"] == ["99"]
        assert result["failed"] == []

    def test_other_errors_go_to_failed(self, tmp_config_dir: Path) -> None:
        """Auth/network/unknown errors land in 'failed' with the API message attached."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.kill_job.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
        )

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["1"])

        assert result["failed"] == [{"id": "1", "error": "Token expired"}]
        assert result["killed"] == []

    def test_batch_tolerates_partial_failures(self, tmp_config_dir: Path) -> None:
        """One failure in a batch does not prevent other jobs from being killed."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()

        def kill_side_effect(jid: str) -> dict:
            if jid == "bad":
                raise KeboolaApiError("Boom", status_code=502, error_code="API_ERROR")
            return {"id": jid, "status": "processing", "desiredStatus": "terminating"}

        mock_client.kill_job.side_effect = kill_side_effect

        service = JobService(store, client_factory=lambda url, token: mock_client)
        result = service.terminate_jobs(alias="prod", job_ids=["ok1", "bad", "ok2"])

        assert {entry["id"] for entry in result["killed"]} == {"ok1", "ok2"}
        assert result["failed"] == [{"id": "bad", "error": "Boom"}]

    def test_resolve_job_ids_filters_by_branch_client_side(self, tmp_config_dir: Path) -> None:
        """resolve_job_ids_by_filter applies branch_id filter client-side (Queue API doesn't)."""
        store = _make_job_store_and_project(tmp_config_dir)
        mock_client = MagicMock()
        mock_client.list_jobs.return_value = [
            {"id": "1", "branchId": "100", "status": "processing"},
            {"id": "2", "branchId": "200", "status": "processing"},
            {"id": "3", "branchId": 100, "status": "waiting"},  # numeric variant
        ]

        service = JobService(store, client_factory=lambda url, token: mock_client)
        jobs = service.resolve_job_ids_by_filter(
            alias="prod",
            status="processing",
            branch_id=100,
        )

        assert {j["id"] for j in jobs} == {"1", "3"}

    def test_filter_killable_drops_terminal_jobs(self) -> None:
        """filter_killable() keeps only created/waiting/processing states."""
        jobs = [
            {"id": "1", "status": "processing"},
            {"id": "2", "status": "terminated"},
            {"id": "3", "status": "waiting"},
            {"id": "4", "status": "success"},
            {"id": "5", "status": "created"},
        ]
        kept = JobService.filter_killable(jobs)
        assert {j["id"] for j in kept} == {"1", "3", "5"}


# ---------------------------------------------------------------------------
# Parallel-specific tests: deterministic ordering, unexpected exceptions
# ---------------------------------------------------------------------------


class TestConfigListDeterministicOrder:
    """Tests that list_configs produces deterministic sort order across projects."""

    def test_configs_sorted_by_alias_component_config(self, tmp_config_dir: Path) -> None:
        """Configs from multiple projects are sorted by (project_alias, component_id, config_id)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "z-project",
            ProjectConfig(
                stack_url="https://z.com",
                token="901-zzz-abcdefghijklmnop",
                project_name="Z Project",
                project_id=100,
            ),
        )
        store.add_project(
            "a-project",
            ProjectConfig(
                stack_url="https://a.com",
                token="902-aaa-abcdefghijklmnop",
                project_name="A Project",
                project_id=200,
            ),
        )

        z_components = [
            {
                "id": "keboola.wr-db-snowflake",
                "name": "Snowflake Writer",
                "type": "writer",
                "configurations": [
                    {"id": "202", "name": "Write B", "description": ""},
                    {"id": "201", "name": "Write A", "description": ""},
                ],
            },
        ]
        a_components = [
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Snowflake Extractor",
                "type": "extractor",
                "configurations": [
                    {"id": "102", "name": "Extract B", "description": ""},
                    {"id": "101", "name": "Extract A", "description": ""},
                ],
            },
        ]

        z_client = _make_list_components_client(z_components)
        a_client = _make_list_components_client(a_components)

        def factory(url: str, token: str) -> MagicMock:
            if "zzz" in token:
                return z_client
            return a_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        # Run multiple times to verify deterministic ordering
        for _ in range(5):
            result = service.list_configs()
            configs = result["configs"]

            assert len(configs) == 4
            # a-project configs should come before z-project configs
            assert configs[0]["project_alias"] == "a-project"
            assert configs[0]["config_id"] == "101"
            assert configs[1]["project_alias"] == "a-project"
            assert configs[1]["config_id"] == "102"
            assert configs[2]["project_alias"] == "z-project"
            assert configs[2]["config_id"] == "201"
            assert configs[3]["project_alias"] == "z-project"
            assert configs[3]["config_id"] == "202"

    def test_configs_sorted_by_component_id_within_project(self, tmp_config_dir: Path) -> None:
        """Within a project, configs are sorted by component_id then config_id."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-prod-abcdefghijklmnop",
                project_name="Prod",
                project_id=1,
            ),
        )

        components = [
            {
                "id": "keboola.wr-db-snowflake",
                "name": "Writer",
                "type": "writer",
                "configurations": [
                    {"id": "301", "name": "W1", "description": ""},
                ],
            },
            {
                "id": "keboola.ex-db-snowflake",
                "name": "Extractor",
                "type": "extractor",
                "configurations": [
                    {"id": "101", "name": "E1", "description": ""},
                ],
            },
        ]

        mock_client = _make_list_components_client(components)
        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        configs = result["configs"]

        assert len(configs) == 2
        # ex-db-snowflake < wr-db-snowflake alphabetically
        assert configs[0]["component_id"] == "keboola.ex-db-snowflake"
        assert configs[1]["component_id"] == "keboola.wr-db-snowflake"


class TestConfigListUnexpectedException:
    """Tests that unexpected (non-KeboolaApiError) exceptions are caught and accumulated."""

    def test_runtime_error_caught_as_unexpected_error(self, tmp_config_dir: Path) -> None:
        """A RuntimeError from the client is caught and accumulated with UNEXPECTED_ERROR."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-broken-abcdefghijklmn",
                project_name="Broken",
                project_id=999,
            ),
        )

        mock_client = MagicMock()
        mock_client.list_components.side_effect = RuntimeError("Something went very wrong")

        service = ConfigService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_configs()
        configs = result["configs"]
        errors = result["errors"]

        assert len(configs) == 0
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "broken"
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"
        assert "Something went very wrong" in errors[0]["message"]

        # Client must still be closed even after unexpected error
        mock_client.close.assert_called_once()

    def test_unexpected_error_with_healthy_project(self, tmp_config_dir: Path) -> None:
        """One project raising RuntimeError does not block healthy projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "good",
            ProjectConfig(
                stack_url="https://good.com",
                token="901-good-abcdefghijklmnop",
                project_name="Good",
                project_id=1,
            ),
        )
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://broken.com",
                token="902-broken-abcdefghijklmn",
                project_name="Broken",
                project_id=2,
            ),
        )

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        broken_client = MagicMock()
        broken_client.list_components.side_effect = RuntimeError("Unexpected crash")

        def factory(url: str, token: str) -> MagicMock:
            if "good" in token:
                return good_client
            return broken_client

        service = ConfigService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_configs()
        configs = result["configs"]
        errors = result["errors"]

        # Good project configs should still be returned
        assert len(configs) == 3
        assert all(c["project_alias"] == "good" for c in configs)

        # Broken project error should be reported
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "broken"
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"

        # Both clients should be closed
        good_client.close.assert_called_once()
        broken_client.close.assert_called_once()


class TestJobListDeterministicOrder:
    """Tests that list_jobs produces deterministic sort order across projects."""

    def test_jobs_sorted_by_alias_and_job_id(self, tmp_config_dir: Path) -> None:
        """Jobs from multiple projects are sorted by (project_alias, job_id)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "z-project",
            ProjectConfig(
                stack_url="https://z.com",
                token="901-zzz-abcdefghijklmnop",
                project_name="Z Project",
                project_id=100,
            ),
        )
        store.add_project(
            "a-project",
            ProjectConfig(
                stack_url="https://a.com",
                token="902-aaa-abcdefghijklmnop",
                project_name="A Project",
                project_id=200,
            ),
        )

        z_jobs = [
            {"id": 3003, "status": "success", "component": "comp-z"},
            {"id": 3001, "status": "error", "component": "comp-z"},
        ]
        a_jobs = [
            {"id": 2002, "status": "success", "component": "comp-a"},
            {"id": 2001, "status": "processing", "component": "comp-a"},
        ]

        z_client = _make_list_jobs_client(z_jobs)
        a_client = _make_list_jobs_client(a_jobs)

        def factory(url: str, token: str) -> MagicMock:
            if "zzz" in token:
                return z_client
            return a_client

        service = JobService(
            config_store=store,
            client_factory=factory,
        )

        # Run multiple times to verify deterministic ordering
        for _ in range(5):
            result = service.list_jobs()
            jobs = result["jobs"]

            assert len(jobs) == 4
            # a-project jobs first (sorted by id as string), then z-project
            assert jobs[0]["project_alias"] == "a-project"
            assert jobs[0]["id"] == 2001
            assert jobs[1]["project_alias"] == "a-project"
            assert jobs[1]["id"] == 2002
            assert jobs[2]["project_alias"] == "z-project"
            assert jobs[2]["id"] == 3001
            assert jobs[3]["project_alias"] == "z-project"
            assert jobs[3]["id"] == 3003


class TestJobListUnexpectedException:
    """Tests that unexpected (non-KeboolaApiError) exceptions are caught in job listing."""

    def test_runtime_error_caught_as_unexpected_error(self, tmp_config_dir: Path) -> None:
        """A RuntimeError from the client is caught with UNEXPECTED_ERROR code."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-broken-abcdefghijklmn",
                project_name="Broken",
                project_id=999,
            ),
        )

        mock_client = MagicMock()
        mock_client.list_jobs.side_effect = RuntimeError("Connection pool exhausted")

        service = JobService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.list_jobs()
        jobs = result["jobs"]
        errors = result["errors"]

        assert len(jobs) == 0
        assert len(errors) == 1
        assert errors[0]["project_alias"] == "broken"
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"
        assert "Connection pool exhausted" in errors[0]["message"]

        # Client must still be closed
        mock_client.close.assert_called_once()

    def test_unexpected_error_does_not_block_healthy_projects(self, tmp_config_dir: Path) -> None:
        """One project with RuntimeError does not block other projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "good",
            ProjectConfig(
                stack_url="https://good.com",
                token="901-good-abcdefghijklmnop",
                project_name="Good",
                project_id=1,
            ),
        )
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://broken.com",
                token="902-broken-abcdefghijklmn",
                project_name="Broken",
                project_id=2,
            ),
        )

        good_client = _make_list_jobs_client(SAMPLE_JOBS)
        broken_client = MagicMock()
        broken_client.list_jobs.side_effect = RuntimeError("Unexpected crash")

        def factory(url: str, token: str) -> MagicMock:
            if "good" in token:
                return good_client
            return broken_client

        service = JobService(
            config_store=store,
            client_factory=factory,
        )

        result = service.list_jobs()
        jobs = result["jobs"]
        errors = result["errors"]

        assert len(jobs) == 2
        assert all(j["project_alias"] == "good" for j in jobs)
        assert len(errors) == 1
        assert errors[0]["error_code"] == "UNEXPECTED_ERROR"

        good_client.close.assert_called_once()
        broken_client.close.assert_called_once()


class TestStatusParallel:
    """Tests for ProjectService.get_status() parallel behaviour."""

    def test_status_multiple_projects_sorted_by_alias(self, tmp_config_dir: Path) -> None:
        """get_status with multiple projects returns results sorted by alias."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "z-project",
            ProjectConfig(
                stack_url="https://z.com",
                token="901-zzz-abcdefghijklmnop",
                project_name="Z Project",
                project_id=100,
            ),
        )
        store.add_project(
            "a-project",
            ProjectConfig(
                stack_url="https://a.com",
                token="902-aaa-abcdefghijklmnop",
                project_name="A Project",
                project_id=200,
            ),
        )
        store.add_project(
            "m-project",
            ProjectConfig(
                stack_url="https://m.com",
                token="903-mmm-abcdefghijklmnop",
                project_name="M Project",
                project_id=300,
            ),
        )

        mock_client = make_mock_client(project_name="Generic", project_id=1)

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        # Run multiple times to verify deterministic ordering
        for _ in range(5):
            result = service.get_status()

            assert len(result) == 3
            assert result[0]["alias"] == "a-project"
            assert result[1]["alias"] == "m-project"
            assert result[2]["alias"] == "z-project"
            assert all(r["status"] == "ok" for r in result)

    def test_status_keboola_api_error_produces_status_error(self, tmp_config_dir: Path) -> None:
        """KeboolaApiError produces a status='error' entry (not in errors list)."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-bad-abcdefghijklmnopq",
                project_name="Bad",
                project_id=1,
            ),
        )

        mock_client = make_failing_client(
            KeboolaApiError(
                message="Token expired",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
        )

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_status()

        # KeboolaApiError returns a status entry (3-tuple), not an error
        assert len(result) == 1
        assert result[0]["alias"] == "bad"
        assert result[0]["status"] == "error"
        assert result[0]["error_code"] == "INVALID_TOKEN"
        assert "Token expired" in result[0]["error"]
        assert "response_time_ms" in result[0]

    def test_status_unexpected_runtime_error(self, tmp_config_dir: Path) -> None:
        """Unexpected RuntimeError produces an error-like status entry."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "broken",
            ProjectConfig(
                stack_url="https://broken.com",
                token="901-broken-abcdefghijklmn",
                project_name="Broken",
                project_id=999,
            ),
        )

        mock_client = MagicMock()
        mock_client.verify_token.side_effect = RuntimeError("DNS resolution failed")

        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: mock_client,
        )

        result = service.get_status()

        # Unexpected errors are converted to status entries with status='error'
        assert len(result) == 1
        assert result[0]["alias"] == "broken"
        assert result[0]["status"] == "error"
        assert result[0]["error_code"] == "UNEXPECTED_ERROR"
        assert "DNS resolution failed" in result[0]["error"]

    def test_status_mixed_ok_api_error_runtime_error(self, tmp_config_dir: Path) -> None:
        """get_status handles a mix of OK, KeboolaApiError, and RuntimeError projects."""
        store = ConfigStore(config_dir=tmp_config_dir)
        store.add_project(
            "a-ok",
            ProjectConfig(
                stack_url="https://ok.com",
                token="901-ok-abcdefghijklmnopq",
                project_name="OK Project",
                project_id=1,
            ),
        )
        store.add_project(
            "b-expired",
            ProjectConfig(
                stack_url="https://expired.com",
                token="902-expired-abcdefghijkl",
                project_name="Expired Project",
                project_id=2,
            ),
        )
        store.add_project(
            "c-crash",
            ProjectConfig(
                stack_url="https://crash.com",
                token="903-crash-abcdefghijklmno",
                project_name="Crash Project",
                project_id=3,
            ),
        )

        ok_client = make_mock_client(project_name="OK Project", project_id=1)
        expired_client = make_failing_client(
            KeboolaApiError(
                message="Token expired",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
        )
        crash_client = MagicMock()
        crash_client.verify_token.side_effect = RuntimeError("Segfault simulation")

        def factory(url: str, token: str) -> MagicMock:
            if "ok" in token:
                return ok_client
            elif "expired" in token:
                return expired_client
            return crash_client

        service = ProjectService(
            config_store=store,
            client_factory=factory,
        )

        result = service.get_status()

        assert len(result) == 3

        # Sorted by alias: a-ok, b-expired, c-crash
        assert result[0]["alias"] == "a-ok"
        assert result[0]["status"] == "ok"
        assert result[0]["project_name"] == "OK Project"

        assert result[1]["alias"] == "b-expired"
        assert result[1]["status"] == "error"
        assert result[1]["error_code"] == "INVALID_TOKEN"

        assert result[2]["alias"] == "c-crash"
        assert result[2]["status"] == "error"
        assert result[2]["error_code"] == "UNEXPECTED_ERROR"
        assert "Segfault simulation" in result[2]["error"]
