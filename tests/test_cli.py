"""Tests for CLI commands via CliRunner - project, config, context, doctor commands."""

import json
import os
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from helpers import make_mock_client
from keboola_agent_cli.auth.sentinel import make_session_token
from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.constants import ENV_CONVERSATION_ID
from keboola_agent_cli.errors import ConfigError, KeboolaApiError
from keboola_agent_cli.http_base import BaseHttpClient
from keboola_agent_cli.models import AppConfig, ProjectConfig
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.job_service import JobService
from keboola_agent_cli.services.lineage_service import LineageService
from keboola_agent_cli.services.project_service import ProjectService

TEST_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"

runner = CliRunner()


class TestProjectAdd:
    """Tests for `kbagent project add` command."""

    def test_project_add_success_json(self, tmp_path: Path) -> None:
        """project add with --json outputs structured success response."""
        mock_client = make_mock_client(project_name="Prod Project", project_id=5678)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "prod",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "prod"
        assert output["data"]["project_name"] == "Prod Project"
        assert output["data"]["project_id"] == 5678
        # Token should be masked
        assert "55555" not in output["data"]["token"]

    def test_project_add_success_human(self, tmp_path: Path) -> None:
        """project add in human mode outputs success message."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "test" in result.output
        assert "Success" in result.output or "Test Project" in result.output

    def test_project_add_invalid_token_exit_code_3(self, tmp_path: Path) -> None:
        """project add with invalid token returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": "invalid-token-abcdefgh"}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "bad",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_project_add_timeout_exit_code_4(self, tmp_path: Path) -> None:
        """project add with network timeout returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        timeout_client = MagicMock()
        timeout_client.verify_token.side_effect = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: timeout_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "timeout",
                ],
            )

        assert result.exit_code == 4


class TestProjectList:
    """Tests for `kbagent project list` command."""

    def test_project_list_json_empty(self, tmp_path: Path) -> None:
        """project list --json with no projects returns empty data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"] == []

    def test_project_list_json_with_projects(self, tmp_path: Path) -> None:
        """project list --json returns project data with masked tokens."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            # Add a project first
            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]) == 1
        assert output["data"][0]["alias"] == "test"
        # Token must be masked
        assert output["data"][0]["token"] != TEST_TOKEN

    def test_project_list_human_mode(self, tmp_path: Path) -> None:
        """project list in human mode outputs a Rich table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="My Production")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "prod",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "prod" in result.output
        assert "Connected Projects" in result.output

    def test_project_list_human_empty(self, tmp_path: Path) -> None:
        """project list in human mode with no projects shows helpful message."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["project", "list"])

        assert result.exit_code == 0
        assert "No projects configured" in result.output


class TestProjectRemove:
    """Tests for `kbagent project remove` command."""

    def test_project_remove_success_json(self, tmp_path: Path) -> None:
        """project remove --json returns structured success."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    "test",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "test"

    def test_project_remove_nonexistent_exit_code_5(self, tmp_path: Path) -> None:
        """project remove with nonexistent alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"


class TestProjectStatus:
    """Tests for `kbagent project status` command."""

    def test_project_status_json(self, tmp_path: Path) -> None:
        """project status --json returns connectivity info."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="Prod", project_id=123)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "prod",
                ],
            )

            result = runner.invoke(app, ["--json", "project", "status"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]) == 1
        assert output["data"][0]["alias"] == "prod"
        assert output["data"][0]["status"] == "ok"
        assert "response_time_ms" in output["data"][0]

    def test_project_status_human(self, tmp_path: Path) -> None:
        """project status in human mode shows status table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                ],
            )

            result = runner.invoke(app, ["project", "status"])

        assert result.exit_code == 0
        assert "Project Status" in result.output


class TestProjectAuthModeColumn:
    """`project list` / `project status` state each project's credential type.

    Before this, the two modes were distinguishable only by accident: the masked
    sentinel renders as ``kbc-...9840``, which reads like a truncated real token.
    """

    STATIC_TOKEN = "901-11111-staticTokenValue1234567"

    def _mixed_config_dir(self, tmp_path: Path) -> Path:
        """A real config dir holding one sentinel and one static project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "session-proj",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=make_session_token(9840),
                project_name="Session Project",
                project_id=9840,
            ),
        )
        store.add_project(
            "static-proj",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token=self.STATIC_TOKEN,
                project_name="Static Project",
                project_id=258,
            ),
        )
        return config_dir

    def _invoke(self, tmp_path: Path, argv: list[str]):
        config_dir = self._mixed_config_dir(tmp_path)
        store = ConfigStore(config_dir=config_dir)
        service = ProjectService(
            config_store=store,
            client_factory=lambda url, token: make_mock_client(),
        )
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = service
            # Rich truncates cells at the 80-column CliRunner default; widen so
            # substring asserts test content rather than the wrap point.
            return runner.invoke(app, argv, env={"COLUMNS": "200"})

    def test_list_json_carries_auth_mode_for_both_types(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ["--json", "project", "list"])

        assert result.exit_code == 0, result.output
        by_alias = {p["alias"]: p for p in json.loads(result.stdout)["data"]}
        assert by_alias["session-proj"]["auth_mode"] == "session"
        assert by_alias["static-proj"]["auth_mode"] == "static"
        # Additive: the pre-existing masked token key is untouched.
        assert by_alias["session-proj"]["token"] == "kbc-...9840"

    def test_list_human_shows_auth_column_and_hides_the_sentinel(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ["project", "list"])

        assert result.exit_code == 0, result.output
        flat = " ".join(result.stdout.split())
        assert "Auth" in flat
        assert "session" in flat
        assert "static" in flat
        # The misleading masked sentinel is not rendered; the static token is.
        assert "kbc-...9840" not in flat
        assert "901-...4567" in flat

    def test_status_json_carries_auth_mode_for_both_types(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ["--json", "project", "status"])

        assert result.exit_code == 0, result.output
        by_alias = {s["alias"]: s for s in json.loads(result.stdout)["data"]}
        assert by_alias["session-proj"]["auth_mode"] == "session"
        assert by_alias["static-proj"]["auth_mode"] == "static"

    def test_status_human_shows_auth_column(self, tmp_path: Path) -> None:
        result = self._invoke(tmp_path, ["project", "status"])

        assert result.exit_code == 0, result.output
        flat = " ".join(result.stdout.split())
        assert "Project Status" in flat
        assert "Auth" in flat
        assert "session" in flat
        assert "static" in flat

    def test_status_single_project_json(self, tmp_path: Path) -> None:
        result = self._invoke(
            tmp_path, ["--json", "project", "status", "--project", "session-proj"]
        )

        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["data"]
        assert len(rows) == 1
        assert rows[0]["auth_mode"] == "session"


class TestProjectListTokenEscaping:
    """Every cell of the projects table is Rich-escaped (nit 14).

    The only injection vector is the user's own config.json, so this pins
    consistency rather than closing a vulnerability -- a token containing Rich
    markup must not be interpreted as styling.
    """

    def test_markup_in_a_stored_token_is_not_interpreted(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)
        # mask_token keeps everything before the first dash, so the markup
        # survives masking and reaches Table.add_row.
        store.add_project(
            "weird",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="[bold red]901-secret-abcdefgh",
                project_name="Weird",
                project_id=7,
            ),
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = store
            MockService.return_value = ProjectService(
                config_store=store,
                client_factory=lambda url, token: make_mock_client(),
            )
            result = runner.invoke(app, ["project", "list"], env={"COLUMNS": "200"})

        assert result.exit_code == 0, result.output
        # Rendered literally rather than swallowed as a style tag.
        assert "[bold red]" in " ".join(result.stdout.split())


class TestProjectUse:
    """Tests for `kbagent project use <alias>` (pin default project)."""

    def _seed(self, config_dir: Path, *aliases: str) -> None:
        """Seed a ConfigStore with one or more projects via the live add path."""
        mock_client = make_mock_client()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store
            MockService.return_value = ProjectService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            for alias in aliases:
                runner.invoke(app, ["project", "add", "--project", alias])

    def test_project_use_pins_alias(self, tmp_path: Path) -> None:
        """project use ALIAS persists default_project to config.json."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod", "stage")

        # default_project should be the first-added (prod); now pin stage.
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            store = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "project", "use", "stage"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["alias"] == "stage"
        assert data["previous"] == "prod"
        # Verify persistence: re-load the store and check default_project.
        persisted = ConfigStore(config_dir=config_dir).load()
        assert persisted.default_project == "stage"

    def test_project_use_unknown_alias_exit_5(self, tmp_path: Path) -> None:
        """project use on an unregistered alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "use", "does-not-exist"])

        assert result.exit_code == 5
        data = json.loads(result.output)
        assert data["status"] == "error"
        assert data["error"]["code"] == "CONFIG_ERROR"

    def test_project_use_human_mode_confirms_pin(self, tmp_path: Path) -> None:
        """project use in human mode prints the new pin."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["project", "use", "prod"])

        assert result.exit_code == 0
        assert "prod" in result.output
        assert "Pinned" in result.output or "pinned" in result.output

    def test_project_current_with_pin(self, tmp_path: Path) -> None:
        """project current reports the persisted pin when no env is set."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("KBAGENT_PROJECT", None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "current"])

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["alias"] == "prod"
        assert data["source"] == "pin"
        assert data["env_override"] is None

    def test_project_current_env_overrides_pin(self, tmp_path: Path) -> None:
        """KBAGENT_PROJECT wins over persisted pin; env presence is reported."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod", "stage")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBAGENT_PROJECT": "stage"}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "current"])

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["alias"] == "stage"
        assert data["source"] == "env"
        assert data["pinned"] == "prod"
        assert data["env_points_to_configured_project"] is True

    def test_project_current_env_points_to_unknown(self, tmp_path: Path) -> None:
        """Unregistered KBAGENT_PROJECT value is still reported, with a flag."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBAGENT_PROJECT": "mystery"}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "current"])

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["alias"] == "mystery"
        assert data["source"] == "env"
        assert data["env_points_to_configured_project"] is False

    def test_project_current_human_mode_with_pin(self, tmp_path: Path) -> None:
        """project current in human mode prints alias + source label."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("KBAGENT_PROJECT", None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["project", "current"])

        assert result.exit_code == 0
        assert "prod" in result.output
        # Rich output should mention the source.
        assert "pin" in result.output.lower()

    def test_project_current_human_mode_env_warns_unknown(self, tmp_path: Path) -> None:
        """Human-mode project current warns when env points to unregistered alias."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBAGENT_PROJECT": "ghost"}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["project", "current"])

        assert result.exit_code == 0
        assert "ghost" in result.output
        # Output should warn the alias is not registered.
        assert "Warning" in result.output or "NOT" in result.output

    def test_project_use_blocked_by_persisted_deny_writes(self, tmp_path: Path) -> None:
        """A persisted policy denying cli:write must block project use (it's a write op)."""
        from keboola_agent_cli.models import PermissionPolicy

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod", "stage")

        # Persist a default-allow policy that denies cli:write.
        store = ConfigStore(config_dir=config_dir)
        cfg = store.load()
        cfg.permissions = PermissionPolicy(mode="allow", allow=[], deny=["cli:write"])
        store.save(cfg)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "use", "stage"])

        assert result.exit_code == 6, result.output
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_project_current_allowed_under_deny_writes(self, tmp_path: Path) -> None:
        """project current is classified read, so cli:write deny must NOT block it."""
        from keboola_agent_cli.models import PermissionPolicy

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir, "prod")

        store = ConfigStore(config_dir=config_dir)
        cfg = store.load()
        cfg.permissions = PermissionPolicy(mode="allow", allow=[], deny=["cli:write"])
        store.save(cfg)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "current"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_project_current_none_set(self, tmp_path: Path) -> None:
        """With no projects and no env, current reports alias=None source=none."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop("KBAGENT_PROJECT", None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "project", "current"])

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["alias"] is None
        assert data["source"] == "none"


class TestFirewallFlags:
    """Tests for top-level --deny-writes / --deny-destructive session flags."""

    def _seed(self, config_dir: Path) -> None:
        mock_client = make_mock_client()
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store
            MockService.return_value = ProjectService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            runner.invoke(app, ["project", "add", "--project", "prod"])

    def test_deny_writes_blocks_project_add(self, tmp_path: Path) -> None:
        """--deny-writes blocks project.add (admin is a superset of cli:write)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                [
                    "--deny-writes",
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "foo",
                    "--url",
                    "https://connection.keboola.com",
                    "--token",
                    TEST_TOKEN,
                ],
            )

        assert result.exit_code == 6
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_writes_allows_read(self, tmp_path: Path) -> None:
        """--deny-writes must not block read operations."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--deny-writes", "--json", "project", "list"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_deny_destructive_blocks_delete_table(self, tmp_path: Path) -> None:
        """--deny-destructive blocks storage.delete-table at the permission callback."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                [
                    "--deny-destructive",
                    "--json",
                    "storage",
                    "delete-table",
                    "--project",
                    "prod",
                    "--table-id",
                    "in.c-x.y",
                    "--yes",
                ],
            )

        assert result.exit_code == 6
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_destructive_allows_admin_ops(self, tmp_path: Path) -> None:
        """--deny-destructive must NOT block admin-tier ops (project.remove, org.setup).

        Documented semantics: --deny-destructive is NARROW (data destruction
        only). Admin operations fall through to --deny-writes, which is the
        wide net. This test locks the contract so a future registry change
        can't widen --deny-destructive's scope silently.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            # project.remove is classified 'admin'; --deny-destructive must
            # NOT block it (permission gate exits 6 if blocked). The command
            # will succeed in removing 'prod' since the seed registers it.
            result = runner.invoke(
                app,
                [
                    "--deny-destructive",
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code != 6, (
            f"--deny-destructive incorrectly blocked admin op project.remove "
            f"(exit {result.exit_code}): {result.output}"
        )
        # Verify the actual operation also succeeded (not just the perm gate).
        data = json.loads(result.output)
        assert data["status"] == "ok"

    def test_deny_writes_blocks_admin_ops(self, tmp_path: Path) -> None:
        """Complement of the above: --deny-writes IS the wide net and DOES block admin."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                [
                    "--deny-writes",
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 6, (
            f"--deny-writes must block admin op project.remove (exit 6); "
            f"got {result.exit_code}: {result.output}"
        )
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_destructive_allows_write(self, tmp_path: Path) -> None:
        """--deny-destructive must NOT block pure 'write' (non-destructive) ops.

        Uses 'permissions check' which evaluates against the PERSISTED policy,
        so the session-only --deny-destructive does not apply there. We instead
        attempt a write op and assert it is not blocked by the permission gate
        (any later error must not be PERMISSION_DENIED).
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        # storage.create-bucket is classified 'write', so --deny-destructive alone
        # does not block it. The command will fail for other reasons (mock client),
        # but the failure code must not be PERMISSION_DENIED.
        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                [
                    "--deny-destructive",
                    "--json",
                    "storage",
                    "create-bucket",
                    "--project",
                    "prod",
                    "--stage",
                    "in",
                    "--name",
                    "b",
                ],
            )

        # The permission check must not fire.
        assert result.exit_code != 6 or "PERMISSION_DENIED" not in result.output

    def test_deny_writes_composes_with_persisted_deny_mode(self, tmp_path: Path) -> None:
        """End-to-end: default-deny policy + --deny-writes still blocks writes.

        A persisted default-deny policy that allows cli:write (unusual but
        syntactically valid) composed with --deny-writes must resolve to
        "write denied" because deny takes precedence over allow in the engine
        (permissions.py: default-deny rule is 'allowed and not denied').
        """
        from keboola_agent_cli.models import PermissionPolicy

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        # Persist a default-deny policy that explicitly allows cli:write.
        store = ConfigStore(config_dir=config_dir)
        cfg = store.load()
        cfg.permissions = PermissionPolicy(mode="deny", allow=["cli:write", "cli:read"], deny=[])
        store.save(cfg)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                [
                    "--deny-writes",
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "newproj",
                ],
            )

        # Merged policy: mode=deny, allow=[cli:write,cli:read], deny=[cli:write].
        # Default-deny rule: allowed AND not denied. cli:write matches both
        # allow and deny -- deny wins.
        assert result.exit_code == 6, result.output
        data = json.loads(result.output)
        assert data["error"]["code"] == "PERMISSION_DENIED"

    def test_deny_writes_and_destructive_merge_with_persisted(self, tmp_path: Path) -> None:
        """Flags merge with persisted policy; never persist to disk."""
        from keboola_agent_cli.models import AppConfig, PermissionPolicy

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        self._seed(config_dir)

        # Persist a policy that only denies branch.delete
        store = ConfigStore(config_dir=config_dir)
        cfg = store.load()
        cfg.permissions = PermissionPolicy(mode="allow", allow=[], deny=["branch.delete"])
        store.save(cfg)

        # Run once with --deny-writes: project.add must be blocked
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(
                app,
                ["--deny-writes", "--json", "project", "add", "--project", "new"],
            )
        assert result.exit_code == 6

        # The persisted policy on disk must NOT have been mutated.
        reloaded: AppConfig = ConfigStore(config_dir=config_dir).load()
        assert reloaded.permissions is not None
        assert reloaded.permissions.deny == ["branch.delete"]


class TestProjectEdit:
    """Tests for `kbagent project edit` command."""

    def test_project_edit_url_json(self, tmp_path: Path) -> None:
        """project edit --url with --json updates URL and returns result."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                    "--url",
                    "https://old.keboola.com",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--project",
                    "test",
                    "--url",
                    "https://new.keboola.com",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["stack_url"] == "https://new.keboola.com"

    def test_project_edit_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """project edit with no changes returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                ],
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--project",
                    "test",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"


# ---------------------------------------------------------------------------
# Helpers for config command tests
# ---------------------------------------------------------------------------

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


def _make_list_components_client(components: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_components returning given data."""
    mock_client = MagicMock()
    mock_client.list_components.return_value = components
    return mock_client


def _setup_config_test(config_dir: Path, projects: dict[str, dict] | None = None):
    """Set up a ConfigStore with given projects for testing config commands.

    Args:
        config_dir: Directory for config files.
        projects: Dict mapping alias to dict with 'token' and optional 'stack_url'.

    Returns:
        Configured ConfigStore instance.
    """
    store = ConfigStore(config_dir=config_dir)
    if projects:
        for alias, info in projects.items():
            store.add_project(
                alias,
                ProjectConfig(
                    stack_url=info.get("stack_url", "https://connection.keboola.com"),
                    token=info["token"],
                    project_name=info.get("project_name", alias),
                    project_id=info.get("project_id", 1234),
                ),
            )
    return store


class TestProjectNameMarkupEscape:
    """Regression: a project name containing square brackets ("[e2e] - ...")
    must reach human-mode output verbatim, not be eaten as a Rich markup tag."""

    BRACKETED_NAME = "[e2e] - kbagent bigquery"

    def test_project_list_human_shows_bracketed_name(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = _setup_config_test(
                config_dir,
                {
                    "bq": {
                        "token": TEST_TOKEN,
                        "project_name": self.BRACKETED_NAME,
                        "project_id": 6100,
                    }
                },
            )
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            # Wide terminal so the Rich table does not wrap the name cell.
            result = runner.invoke(app, ["project", "list"], env={"COLUMNS": "200"})

        assert result.exit_code == 0
        assert "[e2e]" in result.output


class TestConfigList:
    """Tests for `kbagent config list` command."""

    def test_config_list_json_output(self, tmp_path: Path) -> None:
        """config list --json returns structured JSON with configs from all projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        configs = output["data"]["configs"]
        assert len(configs) == 3
        assert configs[0]["project_alias"] == "prod"
        assert configs[0]["component_id"] == "keboola.ex-db-snowflake"
        assert configs[0]["config_name"] == "Production Load"

    def test_config_list_human_output(self, tmp_path: Path) -> None:
        """config list in human mode shows Rich table grouped by project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["config", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Should show project-grouped table
        assert "prod" in result.output
        assert "Configurations" in result.output
        # Rich may truncate long names in narrow terminals
        assert "Product" in result.output
        assert "keboola" in result.output

    def test_config_list_project_filter(self, tmp_path: Path) -> None:
        """config list --project X returns configs only from that project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 3
        assert all(c["project_alias"] == "prod" for c in configs)

    def test_config_list_multiple_projects(self, tmp_path: Path) -> None:
        """config list --project X --project Y returns configs from both."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        prod_client = _make_list_components_client(SAMPLE_COMPONENTS)
        dev_client = _make_list_components_client(SAMPLE_COMPONENTS_2)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        def factory(url, token):
            if "901" in token:
                return prod_client
            return dev_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                    "--project",
                    "dev",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 4  # 3 from prod + 1 from dev
        aliases = {c["project_alias"] for c in configs}
        assert aliases == {"prod", "dev"}

    def test_config_list_type_filter(self, tmp_path: Path) -> None:
        """config list --component-type extractor filters by type."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        # Client returns only extractors when type filter is applied
        extractor_only = [SAMPLE_COMPONENTS[0]]
        mock_client = _make_list_components_client(extractor_only)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--component-type",
                    "extractor",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 2
        assert all(c["component_type"] == "extractor" for c in configs)

    def test_config_list_component_id_filter(self, tmp_path: Path) -> None:
        """config list --component-id X filters by specific component."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client(SAMPLE_COMPONENTS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--component-id",
                    "keboola.wr-db-snowflake",
                ],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        configs = output["data"]["configs"]
        assert len(configs) == 1
        assert configs[0]["component_id"] == "keboola.wr-db-snowflake"

    def test_config_list_unknown_alias_exit_code_5(self, tmp_path: Path) -> None:
        """config list --project unknown returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "not found" in output["error"]["message"]

    def test_config_list_partial_failure_json(self, tmp_path: Path) -> None:
        """config list shows errors for failed projects while returning others."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        bad_client = MagicMock()
        bad_client.list_components.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "good": {"token": "901-good-abcdefghijklmnop"},
                "bad": {"token": "532-bad-abcdefghijklmnopq"},
            },
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"

        configs = output["data"]["configs"]
        errors = output["data"]["errors"]

        assert len(configs) == 3
        assert all(c["project_alias"] == "good" for c in configs)

        assert len(errors) == 1
        assert errors[0]["project_alias"] == "bad"
        assert errors[0]["error_code"] == "INVALID_TOKEN"

    def test_config_list_partial_failure_human(self, tmp_path: Path) -> None:
        """config list in human mode shows warnings for failed projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        good_client = _make_list_components_client(SAMPLE_COMPONENTS)
        bad_client = MagicMock()
        bad_client.list_components.side_effect = KeboolaApiError(
            message="Token expired",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "good": {"token": "901-good-abcdefghijklmnop"},
                "bad": {"token": "532-bad-abcdefghijklmnopq"},
            },
        )

        def factory(url, token):
            if "good" in token:
                return good_client
            return bad_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=factory,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["config", "list"])

        assert result.exit_code == 0
        # Should show configs from good project
        assert "Configurations" in result.output
        # Rich may truncate long names in narrow terminals
        assert "Product" in result.output
        # Should show warning about bad project
        assert "bad" in result.output
        assert "Token expired" in result.output

    def test_config_list_empty_json(self, tmp_path: Path) -> None:
        """config list --json with no configs returns empty data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_components_client([])
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(app, ["--json", "config", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["configs"] == []
        assert output["data"]["errors"] == []

    def test_config_list_invalid_component_type_exit_code_2(self, tmp_path: Path) -> None:
        """config list with invalid --component-type returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--component-type",
                    "invalid-type",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]

    def test_config_list_with_branch_flag(self, tmp_path: Path) -> None:
        """config list --branch 123 --project prod passes branch_id to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = MagicMock()
            config_service.list_configs.return_value = {
                "configs": [],
                "errors": [],
            }
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                    "--branch",
                    "123",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        config_service.list_configs.assert_called_once()
        call_kwargs = config_service.list_configs.call_args
        assert call_kwargs.kwargs.get("branch_id") == 123 or (
            call_kwargs[1].get("branch_id") == 123 if call_kwargs[1] else call_kwargs[0][-1] == 123
        )

    def test_config_list_branch_requires_project(self, tmp_path: Path) -> None:
        """config list --branch 123 without --project returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--branch",
                    "123",
                ],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "INVALID_ARGUMENT" in output["error"]["code"]


class TestConfigDetail:
    """Tests for `kbagent config detail` command."""

    def test_config_detail_json_output(self, tmp_path: Path) -> None:
        """config detail --json returns full config detail."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

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

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["id"] == "101"
        assert output["data"]["name"] == "Production Load"
        assert output["data"]["project_alias"] == "prod"
        assert output["data"]["configuration"] == {"parameters": {"db": "prod"}}

    def test_config_detail_sandbox_annotation(self, tmp_path: Path) -> None:
        """config detail for keboola.sandboxes annotates output with the real Storage workspace ID.

        Regression test for issue #304 bod #3 -- a sandbox config's
        ``parameters.id`` looks like a Storage workspace ID but is in fact
        the sandbox-service-internal handle. Since v0.42.1 (issue #312)
        the enrichment lives in ``ConfigService.get_config_detail``, so
        this test mocks the Storage client's ``list_workspaces`` call to
        return a workspace whose ``configurationId`` matches the sandbox
        config; the service walks the list to resolve the real workspace
        id.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "sb-cfg-1",
            "name": "RO sandbox",
            "componentId": "keboola.sandboxes",
            "configuration": {"parameters": {"id": "1296392806"}},
            "rows": [],
        }
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response
        mock_client.list_workspaces.return_value = [
            {
                "id": 2950518214,
                "component": "keboola.sandboxes",
                "configurationId": "sb-cfg-1",
            },
        ]

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.sandboxes",
                    "--config-id",
                    "sb-cfg-1",
                ],
            )

        assert result.exit_code == 0, f"Exit {result.exit_code}: {result.output}"
        data = json.loads(result.output)["data"]
        annotation = data.get("sandbox_annotation")
        assert annotation is not None, "expected sandbox_annotation in detail output"
        assert annotation["sandbox_service_id"] == "1296392806"
        assert annotation["storage_workspace_id"] == 2950518214
        assert "sandbox-service internal ID" in annotation["note"]
        mock_client.list_workspaces.assert_called_once()

    def test_config_detail_sandbox_annotation_orphan(self, tmp_path: Path) -> None:
        """When no workspace is currently backed by the sandbox config, the annotation
        still appears with ``storage_workspace_id=None`` -- callers can tell the
        difference between "no annotation logic ran" and "annotation ran but no
        workspace was found"."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "sb-cfg-orphan",
            "name": "Orphaned sandbox",
            "componentId": "keboola.sandboxes",
            "configuration": {},  # no parameters.id at all
            "rows": [],
        }
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response
        # No workspace points at sb-cfg-orphan -> service returns None for
        # storage_workspace_id without raising.
        mock_client.list_workspaces.return_value = [
            {
                "id": 9999,
                "component": "keboola.sandboxes",
                "configurationId": "some-other-config",
            },
        ]

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.sandboxes",
                    "--config-id",
                    "sb-cfg-orphan",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        annotation = data["sandbox_annotation"]
        assert annotation["sandbox_service_id"] is None
        assert annotation["storage_workspace_id"] is None

    def test_config_detail_no_sandbox_annotation_for_non_sandbox_component(
        self, tmp_path: Path
    ) -> None:
        """Annotation is keboola.sandboxes-specific; other components must stay clean."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "cfg-101",
            "name": "Snowflake extractor",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [],
        }
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "cfg-101",
                ],
            )

        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert "sandbox_annotation" not in data
        # Service must NOT fan out to list_workspaces for non-sandbox components,
        # even with include_sandbox_annotation=True (which the CLI sets).
        mock_client.list_workspaces.assert_not_called()

    def test_config_detail_human_output(self, tmp_path: Path) -> None:
        """config detail in human mode shows a Rich panel with details."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

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

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Production Load" in result.output
        assert "Configuration Detail" in result.output

    def test_config_detail_unknown_alias_exit_code_5(self, tmp_path: Path) -> None:
        """config detail with unknown alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "nonexistent",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "not found" in output["error"]["message"]

    def test_config_detail_api_error_exit_code(self, tmp_path: Path) -> None:
        """config detail with API error returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Config not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "999",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"

    def test_config_detail_auth_error_exit_code_3(self, tmp_path: Path) -> None:
        """config detail with auth error returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_config_detail_with_branch_flag(self, tmp_path: Path) -> None:
        """config detail --branch 123 passes branch_id to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            config_service = MagicMock()
            config_service.get_config_detail.return_value = {
                "id": "101",
                "name": "Production Load",
                "description": "Loads production data",
                "component_id": "keboola.ex-db-snowflake",
                "project_alias": "prod",
                "configuration": {"parameters": {"db": "prod"}},
                "rows": [],
            }
            MockCfgService.return_value = config_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                    "--branch",
                    "123",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        config_service.get_config_detail.assert_called_once()
        call_kwargs = config_service.get_config_detail.call_args
        assert call_kwargs.kwargs.get("branch_id") == 123 or (
            call_kwargs[1].get("branch_id") == 123 if call_kwargs[1] else call_kwargs[0][-1] == 123
        )

    # --- Bulk mode (--component-id without --config-id) ---------------------

    def test_config_detail_bulk_mode_returns_array_envelope(self, tmp_path: Path) -> None:
        """config detail without --config-id -> {"configs":[...],"errors":[...]}."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        bulk_result = {
            "configs": [
                {
                    "project_alias": "prod",
                    "branch_id": None,
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "101",
                    "name": "cfg A",
                    "configuration": {"parameters": {}},
                    "rows": [],
                },
                {
                    "project_alias": "prod",
                    "branch_id": None,
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "102",
                    "name": "cfg B",
                    "configuration": {"parameters": {}},
                    "rows": [],
                },
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.get_config_detail.return_value = bulk_result
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert "configs" in data
        assert "errors" in data
        assert len(data["configs"]) == 2
        # Verify the service was called without config_id
        call = cfg_service.get_config_detail.call_args
        assert call.kwargs.get("config_id") is None

    def test_config_detail_bulk_mode_multi_project(self, tmp_path: Path) -> None:
        """config detail with multiple --project and no --config-id fans out."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "stage": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        bulk_result = {
            "configs": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "101",
                    "name": "p",
                    "configuration": {},
                    "rows": [],
                },
                {
                    "project_alias": "stage",
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "201",
                    "name": "s",
                    "configuration": {},
                    "rows": [],
                },
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.get_config_detail.return_value = bulk_result
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--project",
                    "stage",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        data = output["data"]
        aliases = {c["project_alias"] for c in data["configs"]}
        assert aliases == {"prod", "stage"}
        # Service received aliases=[prod, stage]
        call = cfg_service.get_config_detail.call_args
        assert list(call.kwargs.get("aliases") or []) == ["prod", "stage"]

    def test_config_detail_config_id_with_multi_project_rejected(self, tmp_path: Path) -> None:
        """--config-id with multiple --project exits 2 (INVALID_ARGUMENT)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "stage": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--project",
                    "stage",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"

    def test_config_detail_single_shape_preserved_for_backward_compat(self, tmp_path: Path) -> None:
        """config detail --config-id XYZ returns the original flat dict shape."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        detail_response = {
            "id": "101",
            "name": "Production Load",
            "componentId": "keboola.ex-db-snowflake",
            "configuration": {"parameters": {"db": "prod"}},
            "rows": [],
        }

        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = detail_response

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        # Back-compat shape: flat dict, NOT wrapped in {"configs": [...]}
        assert output["data"]["id"] == "101"
        assert output["data"]["name"] == "Production Load"
        assert "configs" not in output["data"]

    def test_config_detail_with_state_single_mode(self, tmp_path: Path) -> None:
        """config detail --with-state single mode passes with_state=True to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.get_config_detail.return_value = {
                "id": "101",
                "name": "n",
                "project_alias": "prod",
                "configuration": {},
                "rows": [],
                "state": {"cursor": "abc"},
            }
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                    "--with-state",
                ],
            )

        assert result.exit_code == 0
        call = cfg_service.get_config_detail.call_args
        assert call.kwargs.get("with_state") is True

    def test_config_detail_bulk_with_state_flag(self, tmp_path: Path) -> None:
        """Bulk mode + --with-state forwards with_state=True to the service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        bulk_result = {
            "configs": [
                {
                    "project_alias": "prod",
                    "component_id": "keboola.ex-db-snowflake",
                    "config_id": "101",
                    "name": "n",
                    "configuration": {},
                    "rows": [],
                    "state": {"cursor": "abc"},
                }
            ],
            "errors": [],
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.get_config_detail.return_value = bulk_result
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--with-state",
                ],
            )

        assert result.exit_code == 0
        call = cfg_service.get_config_detail.call_args
        assert call.kwargs.get("with_state") is True
        assert call.kwargs.get("config_id") is None

    def test_config_detail_requires_project(self, tmp_path: Path) -> None:
        """config detail with no --project exits 2 (INVALID_ARGUMENT)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                ],
            )

        assert result.exit_code == 2

    def test_config_detail_rejects_empty_component_id(self, tmp_path: Path) -> None:
        """M3: ``--component-id ""`` fails fast with INVALID_ARGUMENT (exit 2)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "",
                ],
            )

        assert result.exit_code == 2
        # Service must not be called when the fail-fast guard trips.
        cfg_service.get_config_detail.assert_not_called()
        # Also guard against whitespace-only IDs.
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "   ",
                ],
            )
        assert result.exit_code == 2
        cfg_service.get_config_detail.assert_not_called()


class TestConfigListIncludeRows:
    """Tests for `kbagent config list --include-rows` flag."""

    def test_config_list_include_rows_passes_flag_to_service(self, tmp_path: Path) -> None:
        """--include-rows forwards include_rows=True to list_configs."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.list_configs.return_value = {"configs": [], "errors": []}
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "list",
                    "--project",
                    "prod",
                    "--include-rows",
                ],
            )

        assert result.exit_code == 0
        call = cfg_service.list_configs.call_args
        assert call.kwargs.get("include_rows") is True

    def test_config_list_without_include_rows_defaults_false(self, tmp_path: Path) -> None:
        """Default behavior: include_rows=False (backward compat)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            cfg_service = MagicMock()
            cfg_service.list_configs.return_value = {"configs": [], "errors": []}
            MockCfgService.return_value = cfg_service

            result = runner.invoke(
                app,
                ["--json", "config", "list", "--project", "prod"],
            )

        assert result.exit_code == 0
        call = cfg_service.list_configs.call_args
        assert call.kwargs.get("include_rows") is False


# ---------------------------------------------------------------------------
# Job list command tests
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


def _make_list_jobs_client(jobs: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_jobs returning given data."""
    mock_client = MagicMock()
    mock_client.list_jobs.return_value = jobs
    return mock_client


class TestJobList:
    """Tests for `kbagent job list` command."""

    def test_job_list_json_output(self, tmp_path: Path) -> None:
        """job list --json returns structured JSON with jobs from all projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["--json", "job", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        jobs = output["data"]["jobs"]
        assert len(jobs) == 2
        assert jobs[0]["project_alias"] == "prod"
        assert jobs[0]["id"] == 1001
        assert jobs[0]["status"] == "success"

    def test_job_list_human_output(self, tmp_path: Path) -> None:
        """job list in human mode shows Rich table grouped by project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["job", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Jobs" in result.output
        assert "prod" in result.output
        assert "1001" in result.output
        assert "success" in result.output

    def test_job_list_project_filter(self, tmp_path: Path) -> None:
        """job list --project X returns jobs only from that project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client(SAMPLE_JOBS)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--project", "prod"],
            )

        assert result.exit_code == 0
        output = json.loads(result.output)
        jobs = output["data"]["jobs"]
        assert len(jobs) == 2
        assert all(j["project_alias"] == "prod" for j in jobs)

    def test_job_list_invalid_status_exit_code_2(self, tmp_path: Path) -> None:
        """job list --status invalid returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--status", "invalid"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"

    def test_job_list_invalid_limit_exit_code_2(self, tmp_path: Path) -> None:
        """job list --limit 0 returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--limit", "0"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_ARGUMENT"

    def test_job_list_limit_too_high_exit_code_2(self, tmp_path: Path) -> None:
        """job list --limit 501 returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--limit", "501"],
            )

        assert result.exit_code == 2

    def test_job_list_config_id_without_component_id_exit_code_2(self, tmp_path: Path) -> None:
        """job list --config-id without --component-id returns exit code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--config-id", "42"],
            )

        assert result.exit_code == 2
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "component-id" in output["error"]["message"]

    def test_job_list_unknown_project_exit_code_5(self, tmp_path: Path) -> None:
        """job list --project nonexistent returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "list", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_job_list_empty_json(self, tmp_path: Path) -> None:
        """job list with no jobs returns empty list."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_jobs_client([])
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(app, ["--json", "job", "list"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["jobs"] == []
        assert output["data"]["errors"] == []


JOB_DETAIL_RESPONSE = {
    "id": "1001",
    "status": "error",
    "component": "keboola.ex-db-snowflake",
    "config": "123",
    "mode": "run",
    "type": "standard",
    "createdTime": "2026-02-26T10:00:00Z",
    "startTime": "2026-02-26T10:00:05Z",
    "endTime": "2026-02-26T10:00:50Z",
    "durationSeconds": 45,
    "url": "https://queue.keboola.com/jobs/1001",
    "result": {"message": "Validation Error: missing field", "error": {"type": "user"}},
}


class TestJobDetail:
    """Tests for `kbagent job detail` command."""

    def test_job_detail_json_output(self, tmp_path: Path) -> None:
        """job detail --json returns structured JSON with full job data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = dict(JOB_DETAIL_RESPONSE)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "prod", "--job-id", "1001"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["id"] == "1001"
        assert output["data"]["status"] == "error"
        assert output["data"]["project_alias"] == "prod"

    def test_job_detail_human_output(self, tmp_path: Path) -> None:
        """job detail in human mode shows Rich panel."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.return_value = dict(JOB_DETAIL_RESPONSE)

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["job", "detail", "--project", "prod", "--job-id", "1001"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "1001" in result.output
        assert "Validation Error" in result.output

    def test_job_detail_unknown_project_exit_code_5(self, tmp_path: Path) -> None:
        """job detail --project nonexistent returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)
            MockJobService.return_value = JobService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "nonexistent", "--job-id", "1001"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_job_detail_not_found_exit_code_1(self, tmp_path: Path) -> None:
        """job detail for nonexistent job returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_job_detail.side_effect = KeboolaApiError(
            message="Job not found",
            status_code=404,
            error_code="NOT_FOUND",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            job_service = JobService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockJobService.return_value = job_service

            result = runner.invoke(
                app,
                ["--json", "job", "detail", "--project", "prod", "--job-id", "999999"],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "NOT_FOUND"


class TestJobRun:
    """Tests for `kbagent job run` command - branch support."""

    def test_job_run_with_branch_flag(self, tmp_path: Path) -> None:
        """job run --branch 789 passes branch_id to service.run_job."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store

            job_service = MagicMock()
            job_service.run_job.return_value = {
                "id": 555,
                "status": "waiting",
                "branchId": "789",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                    "--branch",
                    "789",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        job_service.run_job.assert_called_once()
        call_kwargs = job_service.run_job.call_args
        assert call_kwargs.kwargs.get("branch_id") == 789 or (
            call_kwargs[1].get("branch_id") == 789 if call_kwargs[1] else False
        )

    def test_job_run_idempotency_key_forwarded(self, tmp_path: Path) -> None:
        """job run --idempotency-key K --force-rerun forwards both to run_job,
        and a replayed result prints the dedup note in human mode (issue #427)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {
                "id": 555,
                "status": "waiting",
                "idempotent_replay": True,
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                    "--idempotency-key",
                    "build-step-1",
                    "--force-rerun",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        call_kwargs = job_service.run_job.call_args.kwargs
        assert call_kwargs.get("idempotency_key") == "build-step-1"
        assert call_kwargs.get("force_rerun") is True
        # Human mode surfaces the replay note when the result was deduplicated.
        assert "prior run" in result.output

    def test_job_run_active_branch_resolved(self, tmp_path: Path) -> None:
        """job run without --branch uses active_branch_id from config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )
        # Set active branch for the project
        store.set_project_branch("prod", 456)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store

            job_service = MagicMock()
            job_service.run_job.return_value = {
                "id": 556,
                "status": "waiting",
                "branchId": "456",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        job_service.run_job.assert_called_once()
        call_kwargs = job_service.run_job.call_args
        assert call_kwargs.kwargs.get("branch_id") == 456 or (
            call_kwargs[1].get("branch_id") == 456 if call_kwargs[1] else False
        )

    def test_job_run_no_branch_passes_none(self, tmp_path: Path) -> None:
        """job run without --branch and no active branch passes branch_id=None."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store

            job_service = MagicMock()
            job_service.run_job.return_value = {
                "id": 557,
                "status": "waiting",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        job_service.run_job.assert_called_once()
        call_kwargs = job_service.run_job.call_args
        assert call_kwargs.kwargs.get("branch_id") is None or (
            call_kwargs[1].get("branch_id") is None if call_kwargs[1] else True
        )

    def test_job_run_branch_requires_project(self, tmp_path: Path) -> None:
        """job run --branch 123 without --project is rejected (but --project is required anyway)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            # --project is required for job run, so missing it returns usage error
            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--branch",
                    "123",
                ],
            )

        # --project is required, so typer returns exit code 2 (usage error)
        assert result.exit_code == 2

    def test_job_run_explicit_variable_values_id_forwarded(self, tmp_path: Path) -> None:
        """--variable-values-id lands in service call as variable_values_id."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {"id": 700, "status": "waiting"}
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                    "--variable-values-id",
                    "row-user-picked",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = job_service.run_job.call_args.kwargs
        assert kwargs["variable_values_id"] == "row-user-picked"
        assert kwargs["no_variables"] is False

    def test_job_run_no_variables_flag_forwarded(self, tmp_path: Path) -> None:
        """--no-variables sets no_variables=True on the service call."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {"id": 701, "status": "waiting"}
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--no-variables",
                ],
            )

        assert result.exit_code == 0, result.output
        kwargs = job_service.run_job.call_args.kwargs
        assert kwargs["no_variables"] is True
        assert kwargs["variable_values_id"] is None
        # resolvedVariableValuesId must be absent when resolution was skipped.
        payload = json.loads(result.output).get("data", {})
        assert "resolvedVariableValuesId" not in payload

    def test_job_run_mode_defaults_to_run(self, tmp_path: Path) -> None:
        """Omitting --mode lands as mode='run' on the service call."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {"id": 702, "status": "waiting"}
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                ],
            )

        assert result.exit_code == 0, result.output
        assert job_service.run_job.call_args.kwargs["mode"] == "run"

    def test_job_run_mode_debug_forwarded(self, tmp_path: Path) -> None:
        """--mode debug reaches the service layer unchanged.

        Locks in that the Queue API ``mode`` body field is opt-in via this
        flag and not silently dropped by the CLI wrapper.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {"id": 703, "status": "waiting"}
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--mode",
                    "debug",
                ],
            )

        assert result.exit_code == 0, result.output
        assert job_service.run_job.call_args.kwargs["mode"] == "debug"

    def test_job_run_mode_rejects_unknown_value(self, tmp_path: Path) -> None:
        """Click's choice gate rejects unsupported --mode values (exit 2).

        Without the Choice gate a bad value would flow through to the wire
        and surface as an opaque 422 from the Queue API.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--mode",
                    "dry-run",
                ],
            )

        assert result.exit_code == 2
        job_service.run_job.assert_not_called()

    def test_job_run_mutually_exclusive_flags_rejected(self, tmp_path: Path) -> None:
        """--variable-values-id + --no-variables is an invalid combination (exit 2)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--variable-values-id",
                    "row-1",
                    "--no-variables",
                ],
            )

        assert result.exit_code == 2
        job_service.run_job.assert_not_called()
        assert "INVALID_ARGUMENT" in result.output

    def test_job_run_rejects_empty_variable_values_id(self, tmp_path: Path) -> None:
        """`--variable-values-id ""` exits 2 with INVALID_ARGUMENT.

        Locks the fail-loud contract: an empty string must not fall through
        to create_job(variable_values_id="") where it would silently be
        omitted from the Queue body.
        """
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--variable-values-id",
                    "",
                ],
            )

        assert result.exit_code == 2
        job_service.run_job.assert_not_called()
        assert "INVALID_ARGUMENT" in result.output
        assert "empty" in result.output.lower()

    def test_job_run_rejects_whitespace_variable_values_id(self, tmp_path: Path) -> None:
        """Whitespace-only `--variable-values-id "   "` also rejected."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-http",
                    "--config-id",
                    "42",
                    "--variable-values-id",
                    "   ",
                ],
            )

        assert result.exit_code == 2
        job_service.run_job.assert_not_called()

    def test_job_run_no_variable_rows_error(self, tmp_path: Path) -> None:
        """Service raises NO_VARIABLE_ROWS → CLI exits 1 with error_code surfaced."""
        from keboola_agent_cli.errors import KeboolaApiError

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.side_effect = KeboolaApiError(
                message="Linked variables config vars-42 has no rows.",
                status_code=0,
                error_code="NO_VARIABLE_ROWS",
            )
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                ],
            )

        assert result.exit_code != 0
        assert "NO_VARIABLE_ROWS" in result.output

    def test_job_run_rich_mode_echoes_resolved_values_id(self, tmp_path: Path) -> None:
        """Rich (non-JSON) output echoes ``resolvedVariableValuesId`` so auto-resolve is visible."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {
                "id": 800,
                "status": "waiting",
                "resolvedVariableValuesId": "row-auto-resolved",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                ],
            )

        assert result.exit_code == 0, result.output
        assert "row-auto-resolved" in result.output
        assert "Bound variable values row" in result.output

    def test_job_run_strips_whitespace_around_variable_values_id(self, tmp_path: Path) -> None:
        """`--variable-values-id '  row-1  '` is trimmed before reaching the service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.run_job.return_value = {"id": 801, "status": "waiting"}
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "run",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.snowflake-transformation",
                    "--config-id",
                    "100",
                    "--variable-values-id",
                    "  row-trimmed  ",
                ],
            )

        assert result.exit_code == 0, result.output
        assert job_service.run_job.call_args.kwargs["variable_values_id"] == "row-trimmed"


class TestJobRunQueuePollingFlags:
    """PR4: --poll-strategy, --log-tail-lines, JOB_TIMEOUT_TERMINATED exit."""

    def _setup(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )
        return store

    def _invoke_job_run(self, store, args, run_job_return=None, run_job_side_effect=None):
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            if run_job_side_effect is not None:
                job_service.run_job.side_effect = run_job_side_effect
            else:
                job_service.run_job.return_value = run_job_return or {
                    "id": 800,
                    "status": "success",
                    "isFinished": True,
                }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(app, args)
        return result, job_service

    def test_poll_strategy_forwarded(self, tmp_path: Path) -> None:
        store = self._setup(tmp_path)
        result, job_service = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--wait",
                "--poll-strategy",
                "fixed",
            ],
        )
        assert result.exit_code == 0, result.output
        assert job_service.run_job.call_args.kwargs["poll_strategy"] == "fixed"

    def test_log_tail_lines_forwarded(self, tmp_path: Path) -> None:
        store = self._setup(tmp_path)
        result, job_service = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--wait",
                "--log-tail-lines",
                "42",
            ],
        )
        assert result.exit_code == 0, result.output
        assert job_service.run_job.call_args.kwargs["log_tail_lines"] == 42

    def test_poll_strategy_invalid_rejected_by_click(self, tmp_path: Path) -> None:
        store = self._setup(tmp_path)
        result, job_service = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--poll-strategy",
                "linear",
            ],
        )
        # Click.Choice rejects before the command body runs.
        assert result.exit_code == 2
        job_service.run_job.assert_not_called()

    def test_log_tail_lines_out_of_range_rejected(self, tmp_path: Path) -> None:
        store = self._setup(tmp_path)
        result, job_service = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--log-tail-lines",
                "99999",
            ],
        )
        assert result.exit_code == 2
        assert "log-tail-lines" in result.output.lower() or "INVALID_ARGUMENT" in result.output
        job_service.run_job.assert_not_called()

    def test_timeout_terminated_exits_seven_with_details(self, tmp_path: Path) -> None:
        """JOB_TIMEOUT_TERMINATED -> exit 7 + details.logTail + details.job in JSON."""
        import json as _json

        from keboola_agent_cli.errors import KeboolaApiError

        store = self._setup(tmp_path)
        result, _ = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--wait",
                "--timeout",
                "5",
            ],
            run_job_side_effect=KeboolaApiError(
                message="timed out; issued kill",
                status_code=504,
                error_code="JOB_TIMEOUT_TERMINATED",
                details={
                    "job": {"id": 900, "status": "terminated", "isFinished": True},
                    "logTail": [{"id": 1, "message": "x"}],
                },
            ),
        )
        assert result.exit_code == 7, result.output
        envelope = _json.loads(result.output)
        assert envelope["status"] == "error"
        assert envelope["error"]["code"] == "JOB_TIMEOUT_TERMINATED"
        assert envelope["error"]["details"]["job"]["status"] == "terminated"
        assert envelope["error"]["details"]["logTail"] == [{"id": 1, "message": "x"}]

    def test_queue_job_timeout_exits_four(self, tmp_path: Path) -> None:
        """Soft gave-up (kill also failed) -> exit 4, retryable=True."""
        import json as _json

        from keboola_agent_cli.errors import KeboolaApiError

        store = self._setup(tmp_path)
        result, _ = self._invoke_job_run(
            store,
            [
                "--json",
                "job",
                "run",
                "--project",
                "prod",
                "--component-id",
                "keboola.ex-http",
                "--config-id",
                "42",
                "--wait",
                "--timeout",
                "5",
            ],
            run_job_side_effect=KeboolaApiError(
                message="did not complete within 5s",
                status_code=504,
                error_code="QUEUE_JOB_TIMEOUT",
                retryable=True,
            ),
        )
        assert result.exit_code == 4
        envelope = _json.loads(result.output)
        assert envelope["error"]["code"] == "QUEUE_JOB_TIMEOUT"
        assert envelope["error"]["retryable"] is True


class TestJobTerminate:
    """Tests for `kbagent job terminate` command."""

    def _setup(self, tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )
        return store

    def test_terminate_single_job_by_id(self, tmp_path: Path) -> None:
        """--job-id variant calls service.terminate_jobs with the exact list."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.terminate_jobs.return_value = {
                "killed": [{"id": "111", "status": "processing", "desiredStatus": "terminating"}],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "dry_run": False,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--job-id",
                    "111",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        job_service.terminate_jobs.assert_called_once()
        call_kwargs = job_service.terminate_jobs.call_args.kwargs
        assert call_kwargs["alias"] == "prod"
        assert call_kwargs["job_ids"] == ["111"]

    def test_terminate_multiple_job_ids(self, tmp_path: Path) -> None:
        """--job-id repeated flags accumulate into a single batch call."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.terminate_jobs.return_value = {
                "killed": [],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "dry_run": False,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--job-id",
                    "a",
                    "--job-id",
                    "b",
                    "--job-id",
                    "c",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        assert job_service.terminate_jobs.call_args.kwargs["job_ids"] == ["a", "b", "c"]

    def test_terminate_requires_job_id_or_status(self, tmp_path: Path) -> None:
        """Providing neither --job-id nor --status is a usage error."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            result = runner.invoke(
                app,
                ["--json", "job", "terminate", "--project", "prod", "--yes"],
            )

        assert result.exit_code == 2

    def test_terminate_rejects_both_job_id_and_status(self, tmp_path: Path) -> None:
        """--job-id and --status are mutually exclusive."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--job-id",
                    "1",
                    "--status",
                    "processing",
                    "--yes",
                ],
            )

        assert result.exit_code == 2

    def test_terminate_rejects_invalid_status(self, tmp_path: Path) -> None:
        """--status success (terminal) is rejected as not killable."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--status",
                    "success",
                    "--yes",
                ],
            )

        assert result.exit_code == 2

    def test_terminate_bulk_by_status(self, tmp_path: Path) -> None:
        """--status triggers resolve_job_ids_by_filter + terminate_jobs with resolved IDs."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.resolve_job_ids_by_filter.return_value = [
                {"id": "1", "status": "processing"},
                {"id": "2", "status": "processing"},
            ]
            job_service.terminate_jobs.return_value = {
                "killed": [
                    {"id": "1", "status": "processing", "desiredStatus": "terminating"},
                    {"id": "2", "status": "processing", "desiredStatus": "terminating"},
                ],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "dry_run": False,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--status",
                    "processing",
                    "--component-id",
                    "keboola.python-transformation-v2",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        job_service.resolve_job_ids_by_filter.assert_called_once()
        job_service.terminate_jobs.assert_called_once_with(alias="prod", job_ids=["1", "2"])

    def test_terminate_status_any_fetches_all_and_filters_client_side(self, tmp_path: Path) -> None:
        """--status any passes status=None to list_jobs and applies filter_killable()."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.resolve_job_ids_by_filter.return_value = [
                {"id": "1", "status": "processing"},
                {"id": "2", "status": "success"},  # should be filtered out
                {"id": "3", "status": "waiting"},
            ]
            job_service.filter_killable.return_value = [
                {"id": "1", "status": "processing"},
                {"id": "3", "status": "waiting"},
            ]
            job_service.terminate_jobs.return_value = {
                "killed": [],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "dry_run": False,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--status",
                    "any",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        # list call should pass status=None (not "any")
        assert job_service.resolve_job_ids_by_filter.call_args.kwargs["status"] is None
        job_service.filter_killable.assert_called_once()
        # terminate_jobs receives only the two killable IDs
        assert job_service.terminate_jobs.call_args.kwargs["job_ids"] == ["1", "3"]

    def test_terminate_dry_run_reports_without_kill(self, tmp_path: Path) -> None:
        """--dry-run short-circuits and forwards dry_run=True to service."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.terminate_jobs.return_value = {
                "killed": [],
                "already_finished": [],
                "not_found": [],
                "failed": [],
                "would_terminate": ["1", "2"],
                "dry_run": True,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--job-id",
                    "1",
                    "--job-id",
                    "2",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        assert job_service.terminate_jobs.call_args.kwargs["dry_run"] is True

    def test_terminate_exit_code_1_on_failures(self, tmp_path: Path) -> None:
        """Non-empty failed bucket yields exit code 1."""
        store = self._setup(tmp_path)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
        ):
            MockStore.return_value = store
            job_service = MagicMock()
            job_service.terminate_jobs.return_value = {
                "killed": [],
                "already_finished": [],
                "not_found": [],
                "failed": [{"id": "1", "error": "timeout"}],
                "dry_run": False,
                "project_alias": "prod",
            }
            MockJobService.return_value = job_service
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "job",
                    "terminate",
                    "--project",
                    "prod",
                    "--job-id",
                    "1",
                    "--yes",
                ],
            )

        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# Context command tests
# ---------------------------------------------------------------------------


class TestContext:
    """Tests for `kbagent context` command."""

    def test_context_output_contains_key_phrases(self, tmp_path: Path) -> None:
        """context command output contains essential phrases for agents."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output
        assert "--json" in result.output
        assert "Exit Codes" in result.output
        assert "project add" in result.output
        assert "config list" in result.output
        assert "Tips for AI Agents" in result.output

    def test_context_json_output(self, tmp_path: Path) -> None:
        """context --json returns structured JSON with context text."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "context"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert "context" in output["data"]
        assert "kbagent" in output["data"]["context"]
        assert "--json" in output["data"]["context"]
        assert "version" in output["data"]

    def test_context_mentions_all_commands(self, tmp_path: Path) -> None:
        """context output mentions all available commands."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        # All major commands should be mentioned
        assert "project add" in result.output
        assert "project list" in result.output
        assert "project remove" in result.output
        assert "project edit" in result.output
        assert "project status" in result.output
        assert "config list" in result.output
        assert "config detail" in result.output
        assert "context" in result.output
        assert "doctor" in result.output

    def test_context_mentions_exit_codes(self, tmp_path: Path) -> None:
        """context output includes exit codes table."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "Authentication error" in result.output
        assert "Network error" in result.output
        assert "Configuration error" in result.output

    def test_context_mentions_workflows(self, tmp_path: Path) -> None:
        """context output includes common workflows."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["context"])

        assert result.exit_code == 0
        assert "Common workflow" in result.output
        assert "Environment variables" in result.output


# ---------------------------------------------------------------------------
# Doctor command tests
# ---------------------------------------------------------------------------


class TestDoctor:
    """Tests for `kbagent doctor` command."""

    def test_doctor_no_config_file(self, tmp_path: Path) -> None:
        """doctor with no config file shows warning for config and skip for parsing."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        checks = output["data"]["checks"]
        assert len(checks) >= 3

        # Config file check should be warn (not found)
        config_check = next(c for c in checks if c["check"] == "config_file")
        assert config_check["status"] == "warn"

        # Config valid check should be skip
        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "skip"

        # Version check should pass
        version_check = next(c for c in checks if c["check"] == "version")
        assert version_check["status"] == "pass"

    def test_doctor_with_valid_config(self, tmp_path: Path) -> None:
        """doctor with a valid config file shows pass for file and valid checks."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "test",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
                project_name="Test",
                project_id=1234,
            ),
        )

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = store
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        # Config file check should pass (file exists with 0600)
        config_check = next(c for c in checks if c["check"] == "config_file")
        assert config_check["status"] == "pass"

        # Config valid check should pass
        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "pass"
        assert "1 project" in valid_check["message"]

    def test_doctor_json_structure(self, tmp_path: Path) -> None:
        """doctor --json returns proper structure with checks and summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"

        data = output["data"]
        assert "checks" in data
        assert "summary" in data
        assert "total" in data["summary"]
        assert "passed" in data["summary"]
        assert "failed" in data["summary"]
        assert "warnings" in data["summary"]
        assert "healthy" in data["summary"]

    def test_doctor_human_output(self, tmp_path: Path) -> None:
        """doctor in human mode shows a Rich panel with check results."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["doctor"])

        assert result.exit_code == 0
        assert "kbagent doctor" in result.output
        assert "WARN" in result.output or "PASS" in result.output or "SKIP" in result.output

    def test_doctor_connectivity_with_mock_client(self, tmp_path: Path) -> None:
        """doctor checks connectivity to projects using the client factory."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-55555-fakeTestTokenDoNotUseXXXXXXXX",
                project_name="Prod",
                project_id=1234,
            ),
        )

        mock_client = make_mock_client(project_name="Prod", project_id=1234)

        # DoctorService now builds its factory via make_client_factory(config_store)
        # so a kbc-session:// project gets a bearer client (0.80.0). The patched
        # name therefore returns the *factory*, not the client.
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.services.doctor_service.make_client_factory") as MockFactory,
        ):
            MockStore.return_value = store
            MockFactory.return_value = lambda _stack_url, _token: mock_client

            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        connectivity_checks = [c for c in checks if c["check"] == "connectivity"]
        assert len(connectivity_checks) == 1
        assert connectivity_checks[0]["status"] == "pass"
        assert "Prod" in connectivity_checks[0]["message"]

    def test_doctor_connectivity_failure(self, tmp_path: Path) -> None:
        """doctor shows fail for projects with connectivity issues."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = ConfigStore(config_dir=config_dir)
        store.add_project(
            "bad",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-badtoken-abcdefghijklmn",
                project_name="Bad",
                project_id=9999,
            ),
        )

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.services.doctor_service.make_client_factory") as MockFactory,
        ):
            MockStore.return_value = store
            MockFactory.return_value = lambda _stack_url, _token: fail_client

            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        connectivity_checks = [c for c in checks if c["check"] == "connectivity"]
        assert len(connectivity_checks) == 1
        assert connectivity_checks[0]["status"] == "fail"
        assert "Invalid token" in connectivity_checks[0]["message"]

    def test_doctor_version_check(self, tmp_path: Path) -> None:
        """doctor always includes a version check that passes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        version_check = next(c for c in checks if c["check"] == "version")
        assert version_check["status"] == "pass"
        assert "kbagent v" in version_check["message"]

    def test_doctor_invalid_json_config(self, tmp_path: Path) -> None:
        """doctor reports fail when config file contains invalid JSON."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_path = config_dir / "config.json"
        config_path.write_text("not valid json {{{", encoding="utf-8")
        config_path.chmod(0o600)

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--json", "doctor"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        checks = output["data"]["checks"]

        valid_check = next(c for c in checks if c["check"] == "config_valid")
        assert valid_check["status"] == "fail"
        assert "not valid JSON" in valid_check["message"]


# ---------------------------------------------------------------------------
# --no-color flag tests
# ---------------------------------------------------------------------------


class TestNoColor:
    """Tests for --no-color global flag."""

    def test_no_color_flag_accepted(self, tmp_path: Path) -> None:
        """--no-color flag is accepted without error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--no-color", "context"])

        assert result.exit_code == 0
        assert "kbagent" in result.output

    def test_no_color_project_list(self, tmp_path: Path) -> None:
        """--no-color works with project list command."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(app, ["--no-color", "project", "list"])

        assert result.exit_code == 0

    def test_no_color_doctor(self, tmp_path: Path) -> None:
        """--no-color works with doctor command."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--no-color", "doctor"])

        assert result.exit_code == 0
        assert "kbagent doctor" in result.output


# ---------------------------------------------------------------------------
# Exit code tests
# ---------------------------------------------------------------------------


class TestExitCodes:
    """Tests for consistent exit codes across commands."""

    def test_auth_error_exit_code_3(self, tmp_path: Path) -> None:
        """Authentication error returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Invalid or expired token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": "invalid-token-abcdefgh"}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "bad",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_network_error_exit_code_4(self, tmp_path: Path) -> None:
        """Network error returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        fail_client = MagicMock()
        fail_client.verify_token.side_effect = KeboolaApiError(
            message="Connection refused",
            status_code=0,
            error_code="CONNECTION_ERROR",
            retryable=True,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: fail_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "unreachable",
                ],
            )

        assert result.exit_code == 4
        output = json.loads(result.output)
        assert output["status"] == "error"

    def test_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """Configuration error returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "remove",
                    "--project",
                    "nonexistent",
                ],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_config_error_exit_code_5_config_detail(self, tmp_path: Path) -> None:
        """Configuration error on config detail returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "nonexistent",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 5

    def test_auth_error_exit_code_3_config_detail(self, tmp_path: Path) -> None:
        """Auth error on config detail returns exit code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Invalid token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 3

    def test_network_error_exit_code_4_config_detail(self, tmp_path: Path) -> None:
        """Network error on config detail returns exit code 4."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = MagicMock()
        mock_client.get_config_detail.side_effect = KeboolaApiError(
            message="Request timed out",
            status_code=0,
            error_code="TIMEOUT",
            retryable=True,
        )

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )

            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "detail",
                    "--project",
                    "prod",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code == 4


# ---------------------------------------------------------------------------
# Help and usage tests
# ---------------------------------------------------------------------------


class TestHelp:
    """Tests for help output on all commands."""

    def test_root_help(self) -> None:
        """Root --help shows app description and command groups."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "project" in result.output
        assert "config" in result.output
        assert "job" in result.output
        assert "context" in result.output
        assert "doctor" in result.output

    def test_project_help(self) -> None:
        """project --help shows subcommands."""
        result = runner.invoke(app, ["project", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output
        assert "list" in result.output
        assert "remove" in result.output
        assert "edit" in result.output
        assert "status" in result.output
        # PR5 additions -- guard against accidental removal.
        assert "use" in result.output
        assert "current" in result.output

    def test_root_callback_registers_firewall_flags(self) -> None:
        """App callback signature declares --deny-writes and --deny-destructive.

        Tests the callback signature rather than rendered --help output: Rich
        truncates options in narrow terminals (CI's default width collapses
        long flag names into '...'), which makes a string-match on the help
        text flaky. The signature is what users see once the terminal has
        room and is a strict superset guarantee.
        """
        import inspect

        from keboola_agent_cli.cli import main as cli_main

        sig = inspect.signature(cli_main)
        assert "deny_writes" in sig.parameters, (
            "cli.main() must accept deny_writes (top-level --deny-writes flag)"
        )
        assert "deny_destructive" in sig.parameters, (
            "cli.main() must accept deny_destructive (top-level --deny-destructive flag)"
        )

    def test_config_help(self) -> None:
        """config --help shows subcommands."""
        result = runner.invoke(app, ["config", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "detail" in result.output

    @staticmethod
    def _strip_ansi(text: str) -> str:
        """Strip ANSI escape codes from text for assertion matching."""
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_project_add_help(self) -> None:
        """project add --help shows all options including --token."""
        result = runner.invoke(app, ["project", "add", "--help"])
        assert result.exit_code == 0
        output = self._strip_ansi(result.output)
        assert "--project" in output
        assert "--url" in output
        assert "--token" in output

    def test_config_list_help(self) -> None:
        """config list --help shows options."""
        result = runner.invoke(app, ["config", "list", "--help"])
        assert result.exit_code == 0
        output = self._strip_ansi(result.output)
        assert "--project" in output
        assert "--component-type" in output
        assert "--component-id" in output

    def test_config_detail_help(self) -> None:
        """config detail --help shows required options."""
        result = runner.invoke(app, ["config", "detail", "--help"])
        assert result.exit_code == 0
        output = self._strip_ansi(result.output)
        assert "--project" in output
        assert "--component-id" in output
        assert "--config-id" in output

    def test_job_help(self) -> None:
        """job --help shows subcommands."""
        result = runner.invoke(app, ["job", "--help"])
        assert result.exit_code == 0
        assert "list" in result.output
        assert "detail" in result.output

    def test_job_list_help(self) -> None:
        """job list --help shows options."""
        result = runner.invoke(app, ["job", "list", "--help"])
        assert result.exit_code == 0
        output = self._strip_ansi(result.output)
        assert "--project" in output
        assert "--component-id" in output
        assert "--config-id" in output
        assert "--status" in output
        assert "--limit" in output


class TestVerboseFlagBasic:
    """Tests for --verbose global flag."""

    def test_verbose_flag_accepted(self, tmp_path: Path) -> None:
        """--verbose flag is accepted without error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--verbose", "context"])

        assert result.exit_code == 0

    def test_verbose_with_json(self, tmp_path: Path) -> None:
        """--verbose and --json can be used together."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["--verbose", "--json", "context"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestMissingRequiredArgs:
    """Tests for missing required arguments."""

    def test_project_add_missing_alias(self, tmp_path: Path) -> None:
        """project add without --project shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                ],
            )

        assert result.exit_code != 0

    def test_project_add_missing_token_non_tty(self, tmp_path: Path) -> None:
        """project add without KBC_TOKEN in non-TTY exits with code 2."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {}, clear=False),
        ):
            # Ensure KBC_TOKEN is not set
            os.environ.pop("KBC_TOKEN", None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                ],
            )

        assert result.exit_code != 0

    def test_project_remove_missing_alias(self, tmp_path: Path) -> None:
        """project remove without --project shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockService.return_value = ProjectService(config_store=MockStore.return_value)

            result = runner.invoke(app, ["project", "remove"])

        assert result.exit_code != 0

    def test_config_detail_missing_project(self, tmp_path: Path) -> None:
        """config detail without --project shows error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
        ):
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            MockProjService.return_value = ProjectService(config_store=MockStore.return_value)
            MockCfgService.return_value = ConfigService(config_store=MockStore.return_value)

            result = runner.invoke(
                app,
                [
                    "config",
                    "detail",
                    "--component-id",
                    "test",
                    "--config-id",
                    "123",
                ],
            )

        assert result.exit_code != 0


class TestProjectEditTokenReverify:
    """Tests for project edit with token changes."""

    def test_project_edit_token_reverify_json(self, tmp_path: Path) -> None:
        """project edit --token triggers re-verification and returns updated info."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        mock_client = make_mock_client(project_name="Original", project_id=100)
        new_mock_client = make_mock_client(project_name="Updated", project_id=200)

        call_count = 0

        def client_factory(url, token):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_client
            return new_mock_client

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=client_factory,
            )
            MockService.return_value = service_instance

            # Add project first
            runner.invoke(
                app,
                [
                    "project",
                    "add",
                    "--project",
                    "test",
                ],
            )

            # Edit with new token via --token flag
            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "edit",
                    "--project",
                    "test",
                    "--token",
                    "new-test-token-456",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"


class TestProjectAddTokenSecurity:
    """Tests for S1: Token input security (env var and interactive prompt)."""

    def test_project_add_token_from_env(self, tmp_path: Path) -> None:
        """Token from KBC_TOKEN env var works for project add."""
        mock_client = make_mock_client(project_name="EnvProject", project_id=999)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "envtest",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "envtest"
        assert output["data"]["project_name"] == "EnvProject"

    def test_project_add_token_interactive(self, tmp_path: Path) -> None:
        """Interactive hidden prompt works for project add when no env var.

        We mock _resolve_token to simulate the interactive prompt returning a token,
        since CliRunner does not have a real TTY and sys.stdin.isatty() is False.
        """
        mock_client = make_mock_client(project_name="PromptProject", project_id=888)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch(
                "keboola_agent_cli.commands.project._resolve_token",
                return_value=TEST_TOKEN,
            ),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "prompttest",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "prompttest"

    def test_project_add_rejects_http_url(self, tmp_path: Path) -> None:
        """http:// URL rejected with error at project add time."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "insecure",
                    "--url",
                    "http://connection.keboola.com",
                ],
            )

        assert result.exit_code != 0, f"Expected failure but got: {result.output}"

    def test_project_add_rejects_file_url(self, tmp_path: Path) -> None:
        """file:// URL rejected with error at project add time."""
        mock_client = make_mock_client()
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "fileurl",
                    "--url",
                    "file:///etc/passwd",
                ],
            )

        assert result.exit_code != 0, f"Expected failure but got: {result.output}"

    def test_project_add_accepts_https_url(self, tmp_path: Path) -> None:
        """https:// URL is accepted at project add time."""
        mock_client = make_mock_client(project_name="SecureProject", project_id=777)
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockService,
            patch.dict(os.environ, {"KBC_TOKEN": TEST_TOKEN}),
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance

            service_instance = ProjectService(
                config_store=store_instance,
                client_factory=lambda url, token: mock_client,
            )
            MockService.return_value = service_instance

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "add",
                    "--project",
                    "secure",
                    "--url",
                    "https://connection.keboola.com",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["alias"] == "secure"


# ---------------------------------------------------------------------------
# Lineage command tests
# ---------------------------------------------------------------------------

SAMPLE_BUCKETS_SHARED = [
    {
        "id": "in.c-shared-data",
        "name": "Shared Data",
        "sharing": "organization-project",
        "linkedBy": [
            {
                "id": "in.c-linked-data",
                "project": {"id": 7012, "name": "Target Project"},
            }
        ],
    },
    {
        "id": "out.c-normal",
        "name": "Normal Bucket",
    },
]

SAMPLE_BUCKETS_EMPTY = [
    {"id": "in.c-data", "name": "Data"},
]


def _make_list_buckets_client(buckets: list[dict]) -> MagicMock:
    """Create a mock KeboolaClient with list_buckets returning given data."""
    mock_client = MagicMock()
    mock_client.list_buckets.return_value = buckets
    return mock_client


class TestSharingEdgesIntegration:
    """Tests for `kbagent sharing edges` command with real LineageService."""

    def test_lineage_json_output(self, tmp_path: Path) -> None:
        """lineage show --json returns structured JSON with edges and summary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["--json", "sharing", "edges"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        data = output["data"]
        assert "edges" in data
        assert len(data["edges"]) >= 1
        assert "summary" in data

    def test_lineage_human_output(self, tmp_path: Path) -> None:
        """lineage show in human mode displays a Rich table with edge data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["sharing", "edges"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Data Flow Edges" in result.output
        # Rich table may truncate long bucket ids; check for prefix or project alias
        assert "in.c-shared" in result.output or "prod" in result.output

    def test_lineage_no_sharing(self, tmp_path: Path) -> None:
        """lineage show with no shared buckets returns empty edges and zero counts."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_EMPTY)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(app, ["--json", "sharing", "edges"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        data = output["data"]
        assert data["edges"] == []
        assert data["summary"]["total_edges"] == 0

    def test_lineage_project_filter(self, tmp_path: Path) -> None:
        """lineage show --project filters to specific project alias."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        mock_client = _make_list_buckets_client(SAMPLE_BUCKETS_SHARED)
        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
                "dev": {"token": "532-abcdef-ghijklmnopqrst"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            lineage_service = LineageService(
                config_store=store,
                client_factory=lambda url, token: mock_client,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(
                app,
                ["--json", "sharing", "edges", "--project", "prod"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

    def test_lineage_config_error(self, tmp_path: Path) -> None:
        """lineage show --project nonexistent returns exit code 5 with CONFIG_ERROR."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            lineage_service = LineageService(
                config_store=store,
            )
            MockLineageService.return_value = lineage_service

            result = runner.invoke(
                app,
                ["--json", "sharing", "edges", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_lineage_no_subcommand_shows_help(self, tmp_path: Path) -> None:
        """lineage without subcommand shows help text."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["lineage"])

        assert result.exit_code == 0
        assert "build" in result.output
        assert "show" in result.output


class TestOrgSetupBasic:
    """Tests for `kbagent org setup` command - basic mock patterns."""

    def _make_org_service_mock(self, result: dict) -> MagicMock:
        """Create a mock OrgService that returns the given result."""
        mock_service = MagicMock()
        mock_service.setup_organization.return_value = result
        return mock_service

    def test_dry_run_json_output(self, tmp_path: Path) -> None:
        """org setup --dry-run with --json outputs structured preview."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {
                    "project_id": 100,
                    "project_name": "Alpha",
                    "alias": "alpha",
                    "action": "would_add",
                },
                {"project_id": 200, "project_name": "Beta", "alias": "beta", "action": "would_add"},
            ],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "123",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert output["data"]["projects_found"] == 2
        assert len(output["data"]["projects_added"]) == 2
        assert output["data"]["projects_added"][0]["action"] == "would_add"

    def test_success_json_output(self, tmp_path: Path) -> None:
        """org setup with --json outputs structured success with added projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {
                    "project_id": 100,
                    "project_name": "Alpha",
                    "alias": "alpha",
                    "token": "901-...ab",
                    "action": "added",
                },
                {
                    "project_id": 200,
                    "project_name": "Beta",
                    "alias": "beta",
                    "token": "901-...cd",
                    "action": "added",
                },
            ],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "123",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is False
        assert len(output["data"]["projects_added"]) == 2
        assert output["data"]["projects_added"][0]["action"] == "added"

    def test_skip_existing_projects(self, tmp_path: Path) -> None:
        """org setup with already registered projects shows them as skipped."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 123,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {
                    "project_id": 200,
                    "project_name": "Beta",
                    "alias": "beta",
                    "token": "901-...cd",
                    "action": "added",
                },
            ],
            "projects_skipped": [
                {
                    "project_id": 100,
                    "project_name": "Alpha",
                    "reason": "Already registered in config",
                },
            ],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "123",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert len(output["data"]["projects_skipped"]) == 1
        assert output["data"]["projects_skipped"][0]["project_id"] == 100

    def test_auth_error_exit_3(self, tmp_path: Path) -> None:
        """org setup with invalid manage token exits with code 3."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        mock_service = MagicMock()
        mock_service.setup_organization.side_effect = KeboolaApiError(
            message="Invalid or expired manage token",
            status_code=401,
            error_code="INVALID_TOKEN",
            retryable=False,
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "123",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 3, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_missing_org_id_and_project_ids_exit_2(self) -> None:
        """org setup without --org-id and --project-ids exits with code 2."""
        with patch(
            "keboola_agent_cli.commands.org.resolve_manage_token",
            return_value="manage-token-123456789012345678",
        ):
            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"

    def test_project_ids_json_output(self, tmp_path: Path) -> None:
        """org setup --project-ids with --json outputs structured success."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        result_data = {
            "organization_id": 438,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {
                    "project_id": 901,
                    "project_name": "Padak",
                    "alias": "padak",
                    "action": "would_add",
                },
                {
                    "project_id": 9621,
                    "project_name": "Padak - BQ/GCS",
                    "alias": "padak-bq-gcs",
                    "action": "would_add",
                },
            ],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": True,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="pat-token-123456789012345678901",
            ),
        ):
            MockStore.return_value = store
            MockOrgService.return_value = self._make_org_service_mock(result_data)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--project-ids",
                    "901,9621",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["projects_found"] == 2
        assert output["data"]["organization_id"] == 438


class TestVerboseFlag:
    """Tests for --verbose flag enabling DEBUG logging."""

    def test_verbose_enables_debug_logging(self, tmp_path: Path) -> None:
        """--verbose sets logging level to DEBUG, output goes to stderr."""
        import logging

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.logging.basicConfig") as mock_basic_config,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockProjService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                ["--json", "--verbose", "project", "list"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Verify logging.basicConfig was called with DEBUG level
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args
        assert call_kwargs[1]["level"] == logging.DEBUG

    def test_default_log_level_is_warning(self, tmp_path: Path) -> None:
        """Without --verbose, logging level defaults to WARNING (no debug noise)."""
        import logging

        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.logging.basicConfig") as mock_basic_config,
        ):
            store_instance = ConfigStore(config_dir=config_dir)
            MockStore.return_value = store_instance
            MockProjService.return_value = ProjectService(config_store=store_instance)

            result = runner.invoke(
                app,
                ["--json", "project", "list"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        # Verify logging.basicConfig was called with WARNING level
        mock_basic_config.assert_called_once()
        call_kwargs = mock_basic_config.call_args
        assert call_kwargs[1]["level"] == logging.WARNING


# ---------------------------------------------------------------------------
# Lineage command tests
# ---------------------------------------------------------------------------


SAMPLE_LINEAGE_RESULT = {
    "edges": [
        {
            "source_project_alias": "prod",
            "source_project_id": 258,
            "source_project_name": "Production",
            "source_bucket_id": "in.c-shared-data",
            "source_bucket_name": "shared-data",
            "sharing_type": "organization-project",
            "target_project_alias": "dev",
            "target_project_id": 7012,
            "target_project_name": "Development",
            "target_bucket_id": "in.c-linked",
        },
    ],
    "shared_buckets": [
        {
            "project_alias": "prod",
            "project_id": 258,
            "project_name": "Production",
            "bucket_id": "in.c-shared-data",
            "bucket_name": "shared-data",
            "sharing_type": "organization-project",
            "shared_by": {},
        },
    ],
    "linked_buckets": [],
    "summary": {
        "total_shared_buckets": 1,
        "total_linked_buckets": 0,
        "total_edges": 1,
        "projects_queried": 2,
        "projects_with_errors": 0,
    },
    "errors": [],
}


class TestSharingEdges:
    """Tests for `kbagent sharing edges` command."""

    def test_lineage_show_json(self, tmp_path: Path) -> None:
        """lineage show --json returns structured JSON with lineage data."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = SAMPLE_LINEAGE_RESULT
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["--json", "sharing", "edges"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["edges"]) == 1
        assert output["data"]["edges"][0]["source_project_alias"] == "prod"
        assert output["data"]["edges"][0]["target_project_alias"] == "dev"
        assert output["data"]["summary"]["total_edges"] == 1
        assert output["data"]["summary"]["projects_queried"] == 2

    def test_lineage_show_human(self, tmp_path: Path) -> None:
        """lineage show in human mode shows Rich table with data flow edges."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = SAMPLE_LINEAGE_RESULT
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["sharing", "edges"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        # Verify human output contains key information
        assert "Data Flow" in result.output or "shared" in result.output
        # Rich may truncate long strings in table columns, so check prefixes
        assert "in.c-shared" in result.output
        assert "organization" in result.output

    def test_lineage_no_subcommand_shows_help(self, tmp_path: Path) -> None:
        """kbagent lineage (without subcommand) shows help with build/show."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        with patch("keboola_agent_cli.cli.ConfigStore") as MockStore:
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, ["lineage"])

        assert result.exit_code == 0
        assert "build" in result.output
        assert "show" in result.output

    def test_lineage_config_error_exit_code_5(self, tmp_path: Path) -> None:
        """lineage with nonexistent project alias returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.side_effect = ConfigError("Project 'nonexistent' not found")
            MockLineageService.return_value = mock_service

            result = runner.invoke(
                app,
                ["--json", "sharing", "edges", "--project", "nonexistent"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_lineage_show_with_warnings(self, tmp_path: Path) -> None:
        """lineage show in human mode displays per-project warnings."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        result_with_errors = {
            "edges": [],
            "shared_buckets": [],
            "linked_buckets": [],
            "summary": {
                "total_shared_buckets": 0,
                "total_linked_buckets": 0,
                "total_edges": 0,
                "projects_queried": 2,
                "projects_with_errors": 1,
            },
            "errors": [
                {
                    "project_alias": "bad",
                    "error_code": "INVALID_TOKEN",
                    "message": "Token expired",
                },
            ],
        }

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.LineageService") as MockLineageService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.get_lineage.return_value = result_with_errors
            MockLineageService.return_value = mock_service

            result = runner.invoke(app, ["sharing", "edges"])

        assert result.exit_code == 0
        assert "bad" in result.output
        assert "Token expired" in result.output


# ---------------------------------------------------------------------------
# Org setup command tests
# ---------------------------------------------------------------------------


SAMPLE_ORG_SETUP_RESULT = {
    "organization_id": 42,
    "stack_url": "https://connection.keboola.com",
    "projects_found": 2,
    "projects_added": [
        {
            "project_id": 100,
            "project_name": "New Project",
            "alias": "new-project",
            "token": "100-***",
            "action": "added",
        },
    ],
    "projects_skipped": [
        {
            "project_id": 200,
            "project_name": "Existing Project",
            "reason": "Already registered in config",
        },
    ],
    "projects_failed": [],
    "dry_run": False,
}

SAMPLE_ORG_DRY_RUN_RESULT = {
    "organization_id": 42,
    "stack_url": "https://connection.keboola.com",
    "projects_found": 2,
    "projects_added": [
        {
            "project_id": 100,
            "project_name": "New Project",
            "alias": "new-project",
            "action": "would_add",
        },
    ],
    "projects_skipped": [
        {
            "project_id": 200,
            "project_name": "Existing Project",
            "reason": "Already registered in config",
        },
    ],
    "projects_failed": [],
    "dry_run": True,
}


class TestOrgSetup:
    """Tests for `kbagent org setup` command."""

    def test_org_setup_dry_run(self, tmp_path: Path) -> None:
        """org setup --dry-run returns structured preview without changes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert len(output["data"]["projects_added"]) == 1
        assert output["data"]["projects_added"][0]["action"] == "would_add"

    def test_org_setup_with_env_token(self, tmp_path: Path) -> None:
        """org setup uses KBC_MANAGE_API_TOKEN env var for authentication
        when the caller opts in via --allow-env-manage-token (since 0.28.0)."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": "manage-test-token"}),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--allow-env-manage-token",
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

    def test_org_setup_confirmation_declined(self, tmp_path: Path) -> None:
        """org setup exits cleanly when user declines confirmation."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            # The preview dry-run call returns projects to add
            mock_service.setup_organization.return_value = SAMPLE_ORG_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            # Simulate user typing "n" to decline confirmation
            result = runner.invoke(
                app,
                [
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                ],
                input="n\n",
            )

        assert result.exit_code == 0
        assert "Aborted" in result.output

    def test_org_setup_api_error(self, tmp_path: Path) -> None:
        """org setup with API error returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.side_effect = KeboolaApiError(
                message="Invalid manage token",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_org_setup_json_with_yes_flag(self, tmp_path: Path) -> None:
        """org setup --json --yes skips confirmation and executes directly."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-abcdef",
            ),
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = SAMPLE_ORG_SETUP_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["organization_id"] == 42
        assert len(output["data"]["projects_added"]) == 1


# ---------------------------------------------------------------------------
# _resolve_manage_token tests
# ---------------------------------------------------------------------------


class TestBranchList:
    """Tests for `kbagent branch list` command."""

    def test_branch_list_success_json(self, tmp_path: Path) -> None:
        """branch list --json returns structured JSON with branches."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"},
            },
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.list_branches.return_value = {
                "branches": [
                    {
                        "project_alias": "prod",
                        "id": 123,
                        "name": "main",
                        "isDefault": True,
                        "created": "2025-01-01T00:00:00Z",
                        "description": "",
                    },
                    {
                        "project_alias": "prod",
                        "id": 456,
                        "name": "feature-x",
                        "isDefault": False,
                        "created": "2025-06-15T10:30:00Z",
                        "description": "Feature branch",
                    },
                ],
                "errors": [],
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(app, ["--json", "branch", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        branches = output["data"]["branches"]
        assert len(branches) == 2
        assert branches[0]["name"] == "main"
        assert branches[0]["isDefault"] is True
        assert branches[1]["name"] == "feature-x"
        assert branches[1]["isDefault"] is False

    def test_branch_list_no_projects(self, tmp_path: Path) -> None:
        """branch list with no projects returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.list_branches.side_effect = ConfigError("Project 'nonexistent' not found.")
            MockBranchService.return_value = mock_branch

            result = runner.invoke(app, ["--json", "branch", "list", "--project", "nonexistent"])

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"

    def test_branch_list_human_output(self, tmp_path: Path) -> None:
        """branch list in human mode shows Rich table with branch names."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.list_branches.return_value = {
                "branches": [
                    {
                        "project_alias": "prod",
                        "id": 123,
                        "name": "main",
                        "isDefault": True,
                        "created": "2025-01-01T00:00:00Z",
                        "description": "",
                    },
                ],
                "errors": [],
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(app, ["branch", "list"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        assert "Development Branches" in result.output
        assert "main" in result.output


class TestResolveManageToken:
    """Tests for resolve_manage_token() in _helpers.py."""

    def test_token_from_env(self) -> None:
        """resolve_manage_token(allow_env=True) returns token from KBC_MANAGE_API_TOKEN env var.

        Default-deny since 0.28.0: callers must pass allow_env=True to opt in
        to env-var resolution. The opt-in is plumbed from the top-level
        --allow-env-manage-token flag.
        """
        from keboola_agent_cli.commands._helpers import resolve_manage_token

        with patch.dict(os.environ, {"KBC_MANAGE_API_TOKEN": "env-manage-token"}):
            token = resolve_manage_token(allow_env=True)

        assert token == "env-manage-token"

    def test_token_from_prompt(self) -> None:
        """resolve_manage_token prompts interactively when env var is not set."""
        import sys

        from keboola_agent_cli.commands._helpers import resolve_manage_token

        with (
            patch.dict(os.environ, {}, clear=False),
            patch(
                "keboola_agent_cli.commands._helpers.typer.prompt",
                return_value="prompted-token",
            ),
            patch.object(sys, "stdin") as mock_stdin,
        ):
            # Ensure KBC_MANAGE_API_TOKEN is not set
            os.environ.pop("KBC_MANAGE_API_TOKEN", None)
            mock_stdin.isatty.return_value = True

            token = resolve_manage_token()

        assert token == "prompted-token"

    def test_non_tty_error(self) -> None:
        """resolve_manage_token raises Exit when not TTY and no env var."""
        import sys

        import typer

        from keboola_agent_cli.commands._helpers import resolve_manage_token

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(sys, "stdin") as mock_stdin,
            pytest.raises(typer.Exit) as exc_info,
        ):
            # Ensure KBC_MANAGE_API_TOKEN is not set
            os.environ.pop("KBC_MANAGE_API_TOKEN", None)
            mock_stdin.isatty.return_value = False

            resolve_manage_token()

        assert exc_info.value.exit_code == 2


# ---------------------------------------------------------------------------
# Branch lifecycle commands (create, use, reset, delete, merge)
# ---------------------------------------------------------------------------


class TestBranchCreate:
    """Tests for `kbagent branch create` command."""

    def test_branch_create_json(self, tmp_path: Path) -> None:
        """branch create --json returns structured JSON with branch details."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.create_branch.return_value = {
                "project_alias": "prod",
                "branch_id": 789,
                "branch_name": "feature-abc",
                "description": "My feature branch",
                "created": "2026-03-03T12:00:00Z",
                "activated": True,
                "message": "Branch 'feature-abc' (ID: 789) created and activated for project 'prod'.",
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                [
                    "--json",
                    "branch",
                    "create",
                    "--project",
                    "prod",
                    "--name",
                    "feature-abc",
                    "--description",
                    "My feature branch",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["branch_id"] == 789
        assert output["data"]["branch_name"] == "feature-abc"
        assert output["data"]["activated"] is True
        assert output["data"]["project_alias"] == "prod"
        mock_branch.create_branch.assert_called_once_with(
            alias="prod", name="feature-abc", description="My feature branch"
        )

    def test_branch_create_api_error(self, tmp_path: Path) -> None:
        """branch create with API error returns exit code 1."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.create_branch.side_effect = KeboolaApiError(
                message="Branch name already exists",
                status_code=400,
                error_code="BRANCH_EXISTS",
                retryable=False,
            )
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                [
                    "--json",
                    "branch",
                    "create",
                    "--project",
                    "prod",
                    "--name",
                    "feature-abc",
                ],
            )

        assert result.exit_code == 1
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "BRANCH_EXISTS"
        assert "Branch name already exists" in output["error"]["message"]


class TestBranchUse:
    """Tests for `kbagent branch use` command."""

    def test_branch_use_json(self, tmp_path: Path) -> None:
        """branch use --json returns structured JSON confirming activation."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.set_active_branch.return_value = {
                "project_alias": "prod",
                "branch_id": 456,
                "branch_name": "feature-x",
                "message": "Active branch set to 'feature-x' (ID: 456) for project 'prod'.",
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "use", "--project", "prod", "--branch", "456"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["branch_id"] == 456
        assert output["data"]["branch_name"] == "feature-x"
        assert output["data"]["project_alias"] == "prod"
        mock_branch.set_active_branch.assert_called_once_with(alias="prod", branch_id=456)

    def test_branch_use_branch_not_found(self, tmp_path: Path) -> None:
        """branch use with nonexistent branch returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.set_active_branch.side_effect = ConfigError(
                "Branch ID 999 not found in project 'prod'. "
                "Use 'kbagent branch list --project prod' to see available branches."
            )
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "use", "--project", "prod", "--branch", "999"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "Branch ID 999 not found" in output["error"]["message"]


class TestBranchReset:
    """Tests for `kbagent branch reset` command."""

    def test_branch_reset_json(self, tmp_path: Path) -> None:
        """branch reset --json returns structured JSON confirming reset."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.reset_branch.return_value = {
                "project_alias": "prod",
                "previous_branch_id": 456,
                "message": "Active branch reset to main for project 'prod'.",
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "reset", "--project", "prod"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["project_alias"] == "prod"
        assert output["data"]["previous_branch_id"] == 456
        assert "reset to main" in output["data"]["message"]
        mock_branch.reset_branch.assert_called_once_with(alias="prod")


class TestBranchDelete:
    """Tests for `kbagent branch delete` command."""

    def test_branch_delete_json(self, tmp_path: Path) -> None:
        """branch delete --json returns structured JSON confirming deletion."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.delete_branch.return_value = {
                "project_alias": "prod",
                "branch_id": 456,
                "was_active": True,
                "message": "Branch ID 456 deleted from project 'prod'. Active branch reset to main.",
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "delete", "--project", "prod", "--branch", "456"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["branch_id"] == 456
        assert output["data"]["was_active"] is True
        assert output["data"]["project_alias"] == "prod"
        assert "deleted" in output["data"]["message"]
        mock_branch.delete_branch.assert_called_once_with(alias="prod", branch_id=456)


class TestBranchMerge:
    """Tests for `kbagent branch merge` command."""

    def test_branch_merge_json(self, tmp_path: Path) -> None:
        """branch merge --json returns structured JSON with merge URL."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.get_merge_url.return_value = {
                "project_alias": "prod",
                "branch_id": 456,
                "url": "https://connection.keboola.com/admin/projects/1234/branch/456/development-overview",
                "message": (
                    "Open this URL to review and merge branch 456 "
                    "in project 'prod'. Active branch has been reset to main."
                ),
            }
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "merge", "--project", "prod", "--branch", "456"],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["branch_id"] == 456
        assert "connection.keboola.com" in output["data"]["url"]
        assert "/branch/456/" in output["data"]["url"]
        assert output["data"]["project_alias"] == "prod"
        mock_branch.get_merge_url.assert_called_once_with(alias="prod", branch_id=456)

    def test_branch_merge_no_branch(self, tmp_path: Path) -> None:
        """branch merge with no active branch and no --branch returns exit code 5."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {"prod": {"token": "901-55555-fakeTestTokenDoNotUseXXXXXXXX"}},
        )

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.ProjectService") as MockProjService,
            patch("keboola_agent_cli.cli.ConfigService") as MockCfgService,
            patch("keboola_agent_cli.cli.JobService") as MockJobService,
            patch("keboola_agent_cli.cli.BranchService") as MockBranchService,
        ):
            MockStore.return_value = store
            MockProjService.return_value = ProjectService(config_store=store)
            MockCfgService.return_value = ConfigService(config_store=store)
            MockJobService.return_value = JobService(config_store=store)

            mock_branch = MagicMock()
            mock_branch.get_merge_url.side_effect = ConfigError(
                "No branch specified and no active branch set for project 'prod'. "
                "Use --branch ID or set an active branch with 'kbagent branch use'."
            )
            MockBranchService.return_value = mock_branch

            result = runner.invoke(
                app,
                ["--json", "branch", "merge", "--project", "prod"],
            )

        assert result.exit_code == 5
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "No branch specified" in output["error"]["message"]


# ---------------------------------------------------------------------------
# Tool auto-resolve active branch
# ---------------------------------------------------------------------------


class TestInit:
    """Tests for `kbagent init` command."""

    def test_init_creates_local_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init creates .kbagent/config.json in CWD."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        result = runner.invoke(app, ["--json", "init"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["created"] is True

        config_path = tmp_path / ".kbagent" / "config.json"
        assert config_path.is_file()

    def test_init_idempotent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """init does not overwrite existing .kbagent/config.json."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        # First init
        runner.invoke(app, ["--json", "init"])
        # Second init
        result = runner.invoke(app, ["--json", "init"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["created"] is False

    def test_init_creates_gitignore(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """init creates/updates .gitignore with .kbagent/ entry."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        runner.invoke(app, ["--json", "init"])

        gitignore = tmp_path / ".gitignore"
        assert gitignore.is_file()
        assert ".kbagent/" in gitignore.read_text(encoding="utf-8")

    def test_init_config_has_claude_warning_first_field(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """config.json starts with a _warning field that steers agents away from REST."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        result = runner.invoke(app, ["--json", "init"])
        assert result.exit_code == 0

        config_path = tmp_path / ".kbagent" / "config.json"
        raw = config_path.read_text(encoding="utf-8")

        # The warning must be the very first JSON key (LLMs read top-down).
        parsed = json.loads(raw)
        first_key = next(iter(parsed.keys()))
        assert first_key == "_warning", (
            f"_warning must be first field; got keys: {list(parsed.keys())}"
        )
        # Warning content must mention the key prohibition.
        warning = parsed["_warning"]
        assert "NEVER" in warning
        assert "Keboola REST API" in warning
        assert "kbagent" in warning

    def test_init_config_warning_ignored_on_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AppConfig.load silently ignores the _warning field -- no crash."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        runner.invoke(app, ["--json", "init"])

        # Round-trip: load and re-save. Warning must still be present after save.
        from keboola_agent_cli.config_store import ConfigStore

        store = ConfigStore(config_dir=tmp_path / ".kbagent", source="local")
        cfg = store.load()  # must not raise
        store.save(cfg)

        raw = (tmp_path / ".kbagent" / "config.json").read_text(encoding="utf-8")
        parsed = json.loads(raw)
        assert parsed["_warning"].startswith("THESE ARE KEBOOLA STORAGE API TOKENS")

    def test_init_from_global_copies_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init --from-global copies projects from global config."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        # Create a global config with a project
        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve,
        ):
            mock_resolve.return_value = (global_dir, "global")

            # Add a project to global config
            from keboola_agent_cli.config_store import ConfigStore
            from keboola_agent_cli.models import ProjectConfig

            global_store = ConfigStore(config_dir=global_dir, source="global")
            global_store.add_project(
                "prod",
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-xxx-testtoken1234",
                    project_name="Production",
                    project_id=1234,
                ),
            )

            result = runner.invoke(app, ["--json", "init", "--from-global"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["projects_copied"] == 1

        # Verify local config has the project
        local_store = ConfigStore(config_dir=tmp_path / ".kbagent")
        local_config = local_store.load()
        assert "prod" in local_config.projects

    def test_init_prompts_to_copy_global_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """init prompts to copy projects when global config has projects (#104)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve,
            patch(
                "keboola_agent_cli.commands.init.typer.confirm", return_value=True
            ) as mock_confirm,
            patch("keboola_agent_cli.commands.init.sys") as mock_sys,
        ):
            mock_resolve.return_value = (global_dir, "global")
            mock_sys.stdin.isatty.return_value = True

            global_store = ConfigStore(config_dir=global_dir, source="global")
            global_store.add_project(
                "prod",
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-xxx-testtoken1234",
                    project_name="Production",
                    project_id=1234,
                ),
            )

            result = runner.invoke(app, ["init"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        mock_confirm.assert_called_once()
        assert "Copy to local workspace?" in mock_confirm.call_args[0][0]

        # Verify local config got the project
        local_store = ConfigStore(config_dir=tmp_path / ".kbagent")
        local_config = local_store.load()
        assert "prod" in local_config.projects

    def test_init_prompt_decline_creates_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Declining the prompt creates empty local config (#104)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with (
            patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve,
            patch("keboola_agent_cli.commands.init.typer.confirm", return_value=False),
            patch("keboola_agent_cli.commands.init.sys") as mock_sys,
        ):
            mock_resolve.return_value = (global_dir, "global")
            mock_sys.stdin.isatty.return_value = True

            global_store = ConfigStore(config_dir=global_dir, source="global")
            global_store.add_project(
                "prod",
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-xxx-testtoken1234",
                    project_name="Production",
                    project_id=1234,
                ),
            )

            result = runner.invoke(app, ["init"])

        assert result.exit_code == 0
        # Local config should have zero projects
        local_store = ConfigStore(config_dir=tmp_path / ".kbagent")
        local_config = local_store.load()
        assert len(local_config.projects) == 0

    def test_init_json_mode_warns_about_global_projects(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In JSON mode, init warns about global projects instead of prompting (#104)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")

            global_store = ConfigStore(config_dir=global_dir, source="global")
            global_store.add_project(
                "prod",
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token="901-xxx-testtoken1234",
                    project_name="Production",
                    project_id=1234,
                ),
            )

            result = runner.invoke(app, ["--json", "init"])

        assert result.exit_code == 0
        output = json.loads(result.output)
        assert output["data"]["projects_copied"] == 0

    def test_empty_local_config_warns_about_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Commands warn when empty local config shadows global with projects (#104)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        # Create an empty local config
        local_dir = tmp_path / ".kbagent"
        local_dir.mkdir()
        local_store = ConfigStore(config_dir=local_dir, source="local")
        local_store.save(AppConfig())

        # Create a global config with projects
        global_dir = tmp_path / "global-config"
        global_dir.mkdir()
        global_store = ConfigStore(config_dir=global_dir, source="global")
        global_store.add_project(
            "prod",
            ProjectConfig(
                stack_url="https://connection.keboola.com",
                token="901-xxx-testtoken1234",
                project_name="Production",
                project_id=1234,
            ),
        )

        with (
            patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve,
            patch("platformdirs.user_config_dir", return_value=str(global_dir)),
            patch("keboola_agent_cli.output.OutputFormatter.warning") as mock_warning,
        ):
            mock_resolve.return_value = (local_dir, "local")

            # Run a non-init command (project list) in human mode
            runner.invoke(app, ["project", "list"])

        # Warning should have been called with message about shadowing
        mock_warning.assert_called_once()
        warning_msg = mock_warning.call_args[0][0]
        assert "Local workspace has no projects but global config has 1" in warning_msg

    @staticmethod
    def _seed_global_with_three(global_dir: Path) -> None:
        """Populate a global config with prod (default), marketing, erp."""
        from keboola_agent_cli.config_store import ConfigStore
        from keboola_agent_cli.models import ProjectConfig

        global_store = ConfigStore(config_dir=global_dir, source="global")
        for alias, project_id in (("prod", 1), ("marketing", 2), ("erp", 3)):
            global_store.add_project(
                alias,
                ProjectConfig(
                    stack_url="https://connection.keboola.com",
                    token=f"901-55555-fakeTestTokenDoNotUse{project_id}",
                    project_name=alias.title(),
                    project_id=project_id,
                ),
            )

    def test_init_from_global_filter_single(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--project copies only the named project and excludes the rest (#404)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            self._seed_global_with_three(global_dir)

            result = runner.invoke(
                app, ["--json", "init", "--from-global", "--project", "marketing"]
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["data"]["projects_copied"] == 1

        local_config = ConfigStore(config_dir=tmp_path / ".kbagent").load()
        assert set(local_config.projects) == {"marketing"}
        # Global default (prod) fell outside the selection -> repointed to marketing.
        assert local_config.default_project == "marketing"

    def test_init_from_global_filter_multiple(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple --project flags copy exactly the named projects (#404)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            self._seed_global_with_three(global_dir)

            result = runner.invoke(
                app,
                [
                    "--json",
                    "init",
                    "--from-global",
                    "--project",
                    "prod",
                    "--project",
                    "erp",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["data"]["projects_copied"] == 2

        local_config = ConfigStore(config_dir=tmp_path / ".kbagent").load()
        assert set(local_config.projects) == {"prod", "erp"}
        # prod is still in the selection, so the global default is preserved.
        assert local_config.default_project == "prod"

    def test_init_project_flag_implies_from_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--project without --from-global still copies the named project (#404)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            self._seed_global_with_three(global_dir)

            result = runner.invoke(app, ["--json", "init", "--project", "erp"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["data"]["projects_copied"] == 1

        local_config = ConfigStore(config_dir=tmp_path / ".kbagent").load()
        assert set(local_config.projects) == {"erp"}

    def test_init_from_global_invalid_alias(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown alias fails with a clear error listing available aliases (#404)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            self._seed_global_with_three(global_dir)

            result = runner.invoke(app, ["--json", "init", "--from-global", "--project", "nope"])

        assert result.exit_code == 5, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["error"]["code"] == "CONFIG_ERROR"
        message = output["error"]["message"]
        assert "nope" in message
        # The error lists the available aliases so the user can correct the typo.
        assert "erp" in message
        assert "marketing" in message
        assert "prod" in message

        # No workspace should have been created on failure.
        assert not (tmp_path / ".kbagent" / "config.json").is_file()

    def test_init_from_global_no_filter_copies_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--from-global without --project preserves copy-all behaviour (#404)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            self._seed_global_with_three(global_dir)

            result = runner.invoke(app, ["--json", "init", "--from-global"])

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["data"]["projects_copied"] == 3

        local_config = ConfigStore(config_dir=tmp_path / ".kbagent").load()
        assert set(local_config.projects) == {"prod", "marketing", "erp"}

    def test_init_from_global_rejects_non_global_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--from-global from a non-global active config fails with CONFIG_ERROR.

        Guards the drive-by fix: the error used to pass message/error_code as
        swapped positional args, so the machine-readable code would have been
        the long prose string instead of "CONFIG_ERROR".
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)

        non_global_dir = tmp_path / "explicit-config"
        non_global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            # Active config resolved from a --config-dir-style override (not global).
            mock_resolve.return_value = (non_global_dir, "local")
            result = runner.invoke(app, ["--json", "init", "--from-global"])

        assert result.exit_code == 5, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "not the global config" in output["error"]["message"]

    def test_init_from_global_rejects_ephemeral_env_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--project __env__ fails clearly instead of copying a project that
        save() would strip (env-synthesized projects live only in memory).
        """
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("KBAGENT_CONFIG_DIR", raising=False)
        # Activate env-mode so load() injects the ephemeral __env__ project.
        monkeypatch.setenv("KBAGENT_PROJECT_FROM_ENV", "1")
        monkeypatch.setenv("KBC_TOKEN", "901-55555-fakeTestTokenDoNotUseEnv")
        monkeypatch.setenv("KBC_STORAGE_API_URL", "https://connection.keboola.com")

        global_dir = tmp_path / "global-config"
        global_dir.mkdir()

        with patch("keboola_agent_cli.cli.resolve_config_dir") as mock_resolve:
            mock_resolve.return_value = (global_dir, "global")
            result = runner.invoke(app, ["--json", "init", "--from-global", "--project", "__env__"])

        assert result.exit_code == 5, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["error"]["code"] == "CONFIG_ERROR"
        assert "__env__" in output["error"]["message"]
        assert "memory" in output["error"]["message"]
        # No half-built workspace left behind on rejection.
        assert not (tmp_path / ".kbagent" / "config.json").is_file()


# ---------------------------------------------------------------------------
# project refresh tests
# ---------------------------------------------------------------------------

SAMPLE_REFRESH_RESULT = {
    "projects_refreshed": [
        {
            "alias": "prod",
            "project_id": 258,
            "project_name": "Production",
            "token": "258-***refreshed",
        },
    ],
    "projects_valid": [
        {
            "alias": "dev",
            "project_id": 7012,
            "project_name": "Development",
        },
    ],
    "projects_skipped": [],
    "projects_failed": [],
    "dry_run": False,
}

SAMPLE_REFRESH_DRY_RUN_RESULT = {
    "projects_refreshed": [
        {
            "alias": "prod",
            "project_id": 258,
            "project_name": "Production",
        },
    ],
    "projects_valid": [
        {
            "alias": "dev",
            "project_id": 7012,
            "project_name": "Development",
        },
    ],
    "projects_skipped": [],
    "projects_failed": [],
    "dry_run": True,
}


class TestProjectRefresh:
    """Tests for `kbagent project refresh` command."""

    def test_project_refresh_json_success(self, tmp_path: Path) -> None:
        """project refresh --all --json --yes returns structured JSON with refreshed projects."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.refresh_tokens.return_value = SAMPLE_REFRESH_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--all",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["projects_refreshed"]) == 1
        assert output["data"]["projects_refreshed"][0]["alias"] == "prod"
        assert output["data"]["projects_refreshed"][0]["project_id"] == 258
        assert output["data"]["dry_run"] is False

    def test_project_refresh_requires_project_or_all(self, tmp_path: Path) -> None:
        """project refresh without --project or --all returns usage error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService"),
        ):
            MockStore.return_value = store

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--yes",
                ],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "--project" in output["error"]["message"] or "--all" in output["error"]["message"]

    def test_project_refresh_both_project_and_all(self, tmp_path: Path) -> None:
        """project refresh with both --project and --all returns usage error."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService"),
        ):
            MockStore.return_value = store

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--project",
                    "prod",
                    "--all",
                    "--yes",
                ],
            )

        assert result.exit_code == 2, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert "not both" in output["error"]["message"]

    def test_project_refresh_dry_run(self, tmp_path: Path) -> None:
        """project refresh --all --dry-run --json shows preview without changes."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.refresh_tokens.return_value = SAMPLE_REFRESH_DRY_RUN_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--all",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert output["data"]["dry_run"] is True
        assert len(output["data"]["projects_refreshed"]) == 1
        assert output["data"]["projects_refreshed"][0]["alias"] == "prod"

    def test_project_refresh_single_project(self, tmp_path: Path) -> None:
        """project refresh --project prod --json --yes refreshes a specific project."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        single_result = {
            "projects_refreshed": [
                {
                    "alias": "prod",
                    "project_id": 258,
                    "project_name": "Production",
                    "token": "258-***refreshed",
                },
            ],
            "projects_valid": [],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.refresh_tokens.return_value = single_result
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--project",
                    "prod",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"
        assert len(output["data"]["projects_refreshed"]) == 1
        assert output["data"]["projects_refreshed"][0]["alias"] == "prod"

        # Verify service was called with aliases=["prod"]
        mock_service.refresh_tokens.assert_called_once()
        call_kwargs = mock_service.refresh_tokens.call_args[1]
        assert call_kwargs["aliases"] == ["prod"]

    def test_project_refresh_api_error(self, tmp_path: Path) -> None:
        """project refresh with API error returns appropriate exit code."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.refresh_tokens.side_effect = KeboolaApiError(
                message="Invalid manage token",
                status_code=401,
                error_code="INVALID_TOKEN",
                retryable=False,
            )
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--all",
                    "--yes",
                ],
            )

        assert result.exit_code == 3
        output = json.loads(result.output)
        assert output["status"] == "error"
        assert output["error"]["code"] == "INVALID_TOKEN"

    def test_project_refresh_force_flag(self, tmp_path: Path) -> None:
        """project refresh --all --force passes force=True to service."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = ConfigStore(config_dir=config_dir)

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.project.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.refresh_tokens.return_value = SAMPLE_REFRESH_RESULT
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "project",
                    "refresh",
                    "--all",
                    "--force",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"

        # Verify force=True was passed to service
        mock_service.refresh_tokens.assert_called_once()
        call_kwargs = mock_service.refresh_tokens.call_args[1]
        assert call_kwargs["force"] is True

    def test_org_setup_refresh_flag(self, tmp_path: Path) -> None:
        """org setup --refresh calls refresh_tokens for skipped projects after setup."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()

        store = _setup_config_test(
            config_dir,
            {
                "existing-project": {
                    "token": "200-oldtoken",
                    "project_id": 200,
                    "project_name": "Existing Project",
                },
            },
        )

        setup_result = {
            "organization_id": 42,
            "stack_url": "https://connection.keboola.com",
            "projects_found": 2,
            "projects_added": [
                {
                    "project_id": 100,
                    "project_name": "New Project",
                    "alias": "new-project",
                    "token": "100-***",
                    "action": "added",
                },
            ],
            "projects_skipped": [
                {
                    "project_id": 200,
                    "project_name": "Existing Project",
                    "reason": "Already registered in config",
                },
            ],
            "projects_failed": [],
            "dry_run": False,
        }

        refresh_result = {
            "projects_refreshed": [
                {
                    "alias": "existing-project",
                    "project_id": 200,
                    "project_name": "Existing Project",
                    "token": "200-***refreshed",
                },
            ],
            "projects_valid": [],
            "projects_skipped": [],
            "projects_failed": [],
            "dry_run": False,
        }

        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch("keboola_agent_cli.cli.OrgService") as MockOrgService,
            patch(
                "keboola_agent_cli.commands.org.resolve_manage_token",
                return_value="manage-token-123456789012345678",
            ),
        ):
            MockStore.return_value = store

            mock_service = MagicMock()
            mock_service.setup_organization.return_value = setup_result
            mock_service.refresh_tokens.return_value = refresh_result
            MockOrgService.return_value = mock_service

            result = runner.invoke(
                app,
                [
                    "--json",
                    "org",
                    "setup",
                    "--org-id",
                    "42",
                    "--url",
                    "https://connection.keboola.com",
                    "--refresh",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, f"Exit code {result.exit_code}: {result.output}"
        output = json.loads(result.output)
        assert output["status"] == "ok"

        # Both setup and refresh should have been called
        mock_service.setup_organization.assert_called_once()
        mock_service.refresh_tokens.assert_called_once()

        # The final result should contain projects_refreshed from the refresh call
        assert len(output["data"]["projects_refreshed"]) == 1
        assert output["data"]["projects_refreshed"][0]["alias"] == "existing-project"


class TestConversationIdFlag:
    """Tests for the top-level --conversation-id session flag (issue #716)."""

    def _run(self, argv: list[str], env: dict[str, str], tmp_path: Path):
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, env, clear=False),
        ):
            os.environ.pop(ENV_CONVERSATION_ID, None)
            os.environ.update(env)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            result = runner.invoke(app, argv)
            observed = os.environ.get(ENV_CONVERSATION_ID)
        return result, observed

    def test_flag_sets_the_conversation_id(self, tmp_path: Path) -> None:
        """The flag is the whole point: no `export` needed, so the command
        still begins with `kbagent` and can match a permission allow-rule."""
        _, observed = self._run(
            ["--conversation-id", "kbagent-session-4242", "--json", "project", "list"],
            {},
            tmp_path,
        )
        assert observed == "kbagent-session-4242"

    def test_flag_wins_over_the_env_var(self, tmp_path: Path) -> None:
        """An explicit flag is the more specific instruction."""
        _, observed = self._run(
            ["--conversation-id", "from-flag", "--json", "project", "list"],
            {ENV_CONVERSATION_ID: "from-env"},
            tmp_path,
        )
        assert observed == "from-flag"

    def test_env_var_still_honoured_without_the_flag(self, tmp_path: Path) -> None:
        """The pre-existing channel is unchanged -- this is purely additive."""
        _, observed = self._run(
            ["--json", "project", "list"],
            {ENV_CONVERSATION_ID: "from-env"},
            tmp_path,
        )
        assert observed == "from-env"

    def test_absent_flag_and_env_leaves_it_unset(self, tmp_path: Path) -> None:
        """No flag, no env var -- the header stays omitted as before."""
        _, observed = self._run(["--json", "project", "list"], {}, tmp_path)
        assert observed is None

    def test_flag_reaches_the_outgoing_header(self, tmp_path: Path) -> None:
        """The flag is worthless unless it actually reaches the wire."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(exist_ok=True)
        with (
            patch("keboola_agent_cli.cli.ConfigStore") as MockStore,
            patch.dict(os.environ, {}, clear=False),
        ):
            os.environ.pop(ENV_CONVERSATION_ID, None)
            MockStore.return_value = ConfigStore(config_dir=config_dir)
            runner.invoke(app, ["--conversation-id", "on-the-wire", "--json", "project", "list"])
            client = BaseHttpClient(
                base_url="https://connection.keboola.com",
                token=TEST_TOKEN,
                headers={},
            )
            try:
                assert client._client.headers.get("X-Conversation-ID") == "on-the-wire"
            finally:
                client.close()
