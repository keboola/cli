"""Tests for issue #766 -- the active dev branch must be visible on every path.

A ``branch use`` pin persists across sessions; before this change an agent
driving ``--json`` had NO signal that its reads and writes were routed to a
dev branch. Covered here:

- ``ConfigStore.set_project_branch`` persists / clears the branch NAME
- ``BranchService`` records the name on use/create, exposes ``current_branch``
- ``resolve_branch`` records a ``BranchContext`` on the formatter
- the JSON envelope (success AND error) carries ``branch`` on branch-aware commands ONLY
- ``kbagent branch current`` (CLI + ``GET /branches/current``)
- ``doctor`` WARNs on a pinned project
"""

import json
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from helpers import setup_single_project, setup_two_projects
from keboola_agent_cli.cli import app
from keboola_agent_cli.commands._helpers import resolve_branch
from keboola_agent_cli.config_store import ConfigStore
from keboola_agent_cli.output import BranchContext, OutputFormatter
from keboola_agent_cli.services.branch_service import BranchService
from keboola_agent_cli.services.config_service import ConfigService
from keboola_agent_cli.services.doctor_service import DoctorService
from keboola_agent_cli.services.project_service import ProjectService

runner = CliRunner()

SAMPLE_BRANCHES = [
    {"id": 123, "name": "main", "isDefault": True, "created": "", "description": ""},
    {"id": 456, "name": "feature-x", "isDefault": False, "created": "", "description": ""},
]


def _branch_service(store: ConfigStore) -> BranchService:
    client = MagicMock()
    client.list_dev_branches.return_value = SAMPLE_BRANCHES
    client.create_dev_branch.return_value = {"id": 789, "name": "created-y", "description": ""}
    return BranchService(config_store=store, client_factory=lambda url, token: client)


class TestConfigStoreBranchName:
    def test_set_project_branch_persists_name(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 456
        assert project.active_branch_name == "feature-x"

    def test_reset_clears_name_too(self, tmp_config_dir: Path) -> None:
        """A stale name beside a null ID would be the misleading signal #766 is about."""
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        store.set_project_branch("prod", None, "feature-x")

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id is None
        assert project.active_branch_name is None

    def test_legacy_config_without_name_loads(self, tmp_config_dir: Path) -> None:
        """Configs written before the field existed carry only the ID."""
        store = setup_single_project(tmp_config_dir)
        raw = json.loads(store.config_path.read_text())
        raw["projects"]["prod"]["active_branch_id"] = 456
        raw["projects"]["prod"].pop("active_branch_name", None)
        store.config_path.write_text(json.dumps(raw))

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 456
        assert project.active_branch_name is None


class TestBranchServiceName:
    def test_set_active_branch_records_name(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        _branch_service(store).set_active_branch(alias="prod", branch_id=456)

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_name == "feature-x"

    def test_create_branch_records_name(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        _branch_service(store).create_branch(alias="prod", name="created-y")

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_id == 789
        assert project.active_branch_name == "created-y"

    def test_reset_branch_clears_name(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        svc = _branch_service(store)
        svc.set_active_branch(alias="prod", branch_id=456)
        svc.reset_branch(alias="prod")

        project = store.get_project("prod")
        assert project is not None
        assert project.active_branch_name is None


class TestCurrentBranchService:
    def test_reports_every_project_sorted(self, tmp_config_dir: Path) -> None:
        store = setup_two_projects(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")

        result = _branch_service(store).current_branch()

        assert [row["project_alias"] for row in result["projects"]] == ["dev", "prod"]
        assert result["active_count"] == 1
        assert result["config_path"] == str(store.config_path)
        by_alias = {row["project_alias"]: row for row in result["projects"]}
        assert by_alias["prod"] == {
            "project_alias": "prod",
            "active_branch_id": 456,
            "active_branch_name": "feature-x",
            "is_production": False,
        }
        assert by_alias["dev"]["is_production"] is True
        assert by_alias["dev"]["active_branch_id"] is None

    def test_no_api_call(self, tmp_config_dir: Path) -> None:
        """The check must stay usable offline -- it reads config.json only."""
        store = setup_single_project(tmp_config_dir)
        client = MagicMock()
        svc = BranchService(config_store=store, client_factory=lambda url, token: client)

        svc.current_branch(aliases=["prod"])

        client.list_dev_branches.assert_not_called()

    def test_unknown_alias_raises(self, tmp_config_dir: Path) -> None:
        from keboola_agent_cli.errors import ConfigError

        store = setup_single_project(tmp_config_dir)
        with pytest.raises(ConfigError):
            _branch_service(store).current_branch(aliases=["nope"])


def _formatter(json_mode: bool = False) -> tuple[OutputFormatter, StringIO]:
    """Formatter whose stderr console writes into the returned buffer."""
    formatter = OutputFormatter(json_mode=json_mode)
    stderr = StringIO()
    formatter.err_console = Console(file=stderr, force_terminal=False, color_system=None)
    return formatter, stderr


class TestResolveBranchContext:
    def test_active_pin_records_context_and_names_branch(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        formatter, stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, "prod", None)

        assert (alias, branch_id) == ("prod", 456)
        assert formatter.branch_context == BranchContext(
            id=456,
            source="active",
            project="prod",
            active_branch_id=456,
            active_branch_name="feature-x",
        )
        stderr = stderr_buf.getvalue()
        assert "456 'feature-x'" in stderr
        assert "writes target that branch" in stderr
        assert "branch reset --project prod" in stderr

    def test_explicit_branch_wins_and_still_shows_pin(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        formatter, _stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, "prod", 999)

        assert (alias, branch_id) == ("prod", 999)
        assert formatter.branch_context is not None
        assert formatter.branch_context.id == 999
        assert formatter.branch_context.source == "explicit"
        assert formatter.branch_context.active_branch_id == 456

    def test_production_without_pin(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        formatter, _stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, "prod", None)

        assert (alias, branch_id) == ("prod", None)
        assert formatter.branch_context == BranchContext(
            id=None,
            source="production",
            project="prod",
            active_branch_id=None,
            active_branch_name=None,
        )

    def test_ignored_pin_reports_production_but_keeps_pin_visible(
        self, tmp_config_dir: Path
    ) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        formatter, _stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, "prod", None, ignore_active_branch=True)

        assert (alias, branch_id) == ("prod", None)
        assert formatter.branch_context is not None
        assert formatter.branch_context.source == "production"
        assert formatter.branch_context.id is None
        assert formatter.branch_context.active_branch_id == 456
        assert formatter.branch_context.active_branch_name == "feature-x"

    def test_no_project_single_pin_resolves_alias(self, tmp_config_dir: Path) -> None:
        store = setup_two_projects(tmp_config_dir)
        store.set_project_branch("dev", 456, "feature-x")
        formatter, _stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, None, None)

        assert (alias, branch_id) == ("dev", 456)
        assert formatter.branch_context is not None
        assert formatter.branch_context.project == "dev"

    def test_no_project_two_pins_stays_production(self, tmp_config_dir: Path) -> None:
        """Two pinned projects: picking one would be a guess (pre-#766 behaviour kept)."""
        store = setup_two_projects(tmp_config_dir)
        store.set_project_branch("dev", 456, "feature-x")
        store.set_project_branch("prod", 123, "main")
        formatter, _stderr_buf = _formatter()

        alias, branch_id = resolve_branch(store, formatter, None, None)

        assert (alias, branch_id) == (None, None)
        assert formatter.branch_context is not None
        assert formatter.branch_context.source == "production"
        assert formatter.branch_context.project is None

    def test_json_mode_is_silent_on_stderr(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        formatter, stderr_buf = _formatter(json_mode=True)

        resolve_branch(store, formatter, "prod", None)

        assert stderr_buf.getvalue() == ""
        assert formatter.branch_context is not None


class TestJsonEnvelope:
    def test_branch_key_absent_without_context(self) -> None:
        formatter = OutputFormatter(json_mode=True)
        with patch("keboola_agent_cli.output.write_machine_output") as write:
            formatter.output({"x": None})
        envelope = json.loads(write.call_args.args[0])
        assert "branch" not in envelope
        # A blanket exclude_none would have stripped this null -- it must survive.
        assert envelope["data"] == {"x": None}

    def test_branch_key_present_with_context(self) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.branch_context = BranchContext(
            id=456,
            source="active",
            project="prod",
            active_branch_id=456,
            active_branch_name="feature-x",
        )
        with patch("keboola_agent_cli.output.write_machine_output") as write:
            formatter.output({"id": "101"})
        envelope = json.loads(write.call_args.args[0])
        assert envelope["status"] == "ok"
        assert envelope["data"] == {"id": "101"}
        assert envelope["branch"] == {
            "id": 456,
            "source": "active",
            "project": "prod",
            "active_branch_id": 456,
            "active_branch_name": "feature-x",
        }

    def test_error_envelope_carries_branch(self) -> None:
        """A failed write should still say which branch it was aimed at."""
        formatter = OutputFormatter(json_mode=True)
        formatter.branch_context = BranchContext(
            id=456,
            source="active",
            project="prod",
            active_branch_id=456,
            active_branch_name="feature-x",
        )
        with patch("keboola_agent_cli.output.write_machine_output") as write:
            formatter.error(message="boom", error_code="INVALID_TOKEN")
        envelope = json.loads(write.call_args.args[0])
        assert envelope["status"] == "error"
        assert envelope["branch"]["id"] == 456

    def test_error_envelope_without_context_unchanged(self) -> None:
        formatter = OutputFormatter(json_mode=True)
        with patch("keboola_agent_cli.output.write_machine_output") as write:
            formatter.error(message="boom", error_code="INVALID_TOKEN")
        assert "branch" not in json.loads(write.call_args.args[0])

    def test_success_helper_carries_branch(self) -> None:
        formatter = OutputFormatter(json_mode=True)
        formatter.branch_context = BranchContext(
            id=None,
            source="production",
            project="prod",
            active_branch_id=None,
            active_branch_name=None,
        )
        with patch("keboola_agent_cli.output.write_machine_output") as write:
            formatter.success("done")
        envelope = json.loads(write.call_args.args[0])
        assert envelope["branch"]["source"] == "production"
        assert envelope["data"] == {"message": "done"}


def _config_cli_env(tmp_path: Path, mock_client: MagicMock) -> tuple[ConfigStore, list]:
    """Real ConfigStore + ConfigService over a mocked Storage client."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    store = setup_single_project(config_dir)
    patches = [
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch(
            "keboola_agent_cli.cli.ProjectService", return_value=ProjectService(config_store=store)
        ),
        patch(
            "keboola_agent_cli.cli.ConfigService",
            return_value=ConfigService(
                config_store=store, client_factory=lambda url, token: mock_client
            ),
        ),
    ]
    return store, patches


DETAIL = {
    "id": "101",
    "name": "Production Load",
    "description": "",
    "componentId": "keboola.ex-db-snowflake",
    "configuration": {"parameters": {"db": "prod"}},
    "rows": [],
    "version": 11,
}


class TestConfigCommandsEcho:
    """The two commands the issue names: detail and update (dry-run + apply)."""

    def test_config_detail_json_reports_active_branch(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = DETAIL
        store, patches = _config_cli_env(tmp_path, mock_client)
        store.set_project_branch("prod", 456, "feature-x")

        with patches[0], patches[1], patches[2]:
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

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["branch"]["id"] == 456
        assert envelope["branch"]["source"] == "active"
        assert envelope["branch"]["active_branch_name"] == "feature-x"
        # The request really went to the branch endpoint.
        assert mock_client.get_config_detail.call_args.kwargs.get("branch_id") == 456

    def test_config_detail_json_reports_production(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = DETAIL
        _, patches = _config_cli_env(tmp_path, mock_client)

        with patches[0], patches[1], patches[2]:
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

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["branch"] == {
            "id": None,
            "source": "production",
            "project": "prod",
            "active_branch_id": None,
            "active_branch_name": None,
        }

    def test_config_update_dry_run_json_reports_active_branch(self, tmp_path: Path) -> None:
        """--dry-run used to look identical for main and a branch (issue #766)."""
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = DETAIL
        store, patches = _config_cli_env(tmp_path, mock_client)
        store.set_project_branch("prod", 456, "feature-x")

        with patches[0], patches[1], patches[2]:
            result = runner.invoke(
                app,
                [
                    "--json",
                    "config",
                    "update",
                    "--project",
                    "prod",
                    "--component-id",
                    "keboola.ex-db-snowflake",
                    "--config-id",
                    "101",
                    "--set",
                    "parameters.db=staging",
                    "--dry-run",
                ],
            )

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["branch"]["id"] == 456
        assert envelope["branch"]["source"] == "active"
        mock_client.update_config.assert_not_called()

    def test_config_detail_human_names_the_branch_on_stderr(self, tmp_path: Path) -> None:
        mock_client = MagicMock()
        mock_client.get_config_detail.return_value = DETAIL
        store, patches = _config_cli_env(tmp_path, mock_client)
        store.set_project_branch("prod", 456, "feature-x")

        with patches[0], patches[1], patches[2]:
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

        assert result.exit_code == 0, result.output
        assert "Using active dev branch 456 'feature-x'" in result.output
        assert "branch reset --project prod" in result.output

    def test_non_branch_command_has_no_branch_key(self, tmp_path: Path) -> None:
        """Absence must keep meaning 'not branch-scoped', never 'production'."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = setup_single_project(config_dir)
        store.set_project_branch("prod", 456, "feature-x")

        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch(
                "keboola_agent_cli.cli.ProjectService",
                return_value=ProjectService(config_store=store),
            ),
        ):
            result = runner.invoke(app, ["--json", "project", "list"])

        assert result.exit_code == 0, result.output
        assert "branch" not in json.loads(result.output)


class TestBranchCurrentCli:
    def _env(self, tmp_path: Path) -> tuple[ConfigStore, list]:
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = setup_two_projects(config_dir)
        patches = [
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch(
                "keboola_agent_cli.cli.ProjectService",
                return_value=ProjectService(config_store=store),
            ),
            patch("keboola_agent_cli.cli.BranchService", return_value=_branch_service(store)),
        ]
        return store, patches

    def test_json(self, tmp_path: Path) -> None:
        store, patches = self._env(tmp_path)
        store.set_project_branch("prod", 456, "feature-x")

        with patches[0], patches[1], patches[2]:
            result = runner.invoke(app, ["--json", "branch", "current"])

        assert result.exit_code == 0, result.output
        envelope = json.loads(result.output)
        assert envelope["status"] == "ok"
        assert envelope["data"]["active_count"] == 1
        rows = {row["project_alias"]: row for row in envelope["data"]["projects"]}
        assert rows["prod"]["active_branch_id"] == 456
        assert rows["prod"]["active_branch_name"] == "feature-x"
        assert rows["dev"]["is_production"] is True
        # `branch current` itself is not routed through resolve_branch.
        assert "branch" not in envelope

    def test_human_pinned(self, tmp_path: Path) -> None:
        store, patches = self._env(tmp_path)
        store.set_project_branch("prod", 456, "feature-x")

        with patches[0], patches[1], patches[2]:
            result = runner.invoke(app, ["branch", "current", "--project", "prod"])

        assert result.exit_code == 0, result.output
        assert "dev branch 456 'feature-x'" in result.output
        assert "branch reset --project prod" in result.output
        assert "Pins read from:" in result.output

    def test_human_all_production(self, tmp_path: Path) -> None:
        _, patches = self._env(tmp_path)

        with patches[0], patches[1], patches[2]:
            result = runner.invoke(app, ["branch", "current"])

        assert result.exit_code == 0, result.output
        assert "main (production)" in result.output
        assert "No dev branch is active" in result.output

    def test_unknown_alias_exit_5(self, tmp_path: Path) -> None:
        _, patches = self._env(tmp_path)

        with patches[0], patches[1], patches[2]:
            result = runner.invoke(app, ["--json", "branch", "current", "--project", "nope"])

        assert result.exit_code == 5, result.output
        assert json.loads(result.output)["status"] == "error"

    def test_is_a_read_operation(self) -> None:
        from keboola_agent_cli.permissions import OPERATION_REGISTRY

        assert OPERATION_REGISTRY["branch.current"] == "read"


class TestDoctorActiveDevBranches:
    def test_pass_without_pin(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        check = DoctorService._check_active_dev_branches(store.load())
        assert check["check"] == "active_dev_branches"
        assert check["status"] == "pass"

    def test_warn_with_pin(self, tmp_config_dir: Path) -> None:
        store = setup_two_projects(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")

        check = DoctorService._check_active_dev_branches(store.load())

        assert check["status"] == "warn"
        assert "prod -> 456 'feature-x'" in check["message"]
        assert "branch current" in check["message"]
        assert check["details"]["pinned"] == [
            {"project_alias": "prod", "active_branch_id": 456, "active_branch_name": "feature-x"}
        ]

    def test_pass_when_config_missing(self) -> None:
        assert DoctorService._check_active_dev_branches(None)["status"] == "pass"

    def test_included_in_run_checks(self, tmp_config_dir: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        service = DoctorService(config_store=store)

        with patch.object(service, "_check_connectivity", return_value=[]):
            result = service.run_checks()

        names = [check["check"] for check in result["checks"]]
        assert "active_dev_branches" in names
        assert result["summary"]["healthy"] is True  # warn never breaks health


class TestServeBranchesCurrent:
    def test_get_branches_current(self, tmp_path: Path) -> None:
        pytest.importorskip("fastapi")
        from fastapi.testclient import TestClient

        from keboola_agent_cli.server import create_app
        from keboola_agent_cli.server.dependencies import ServiceRegistry, get_registry

        config_dir = tmp_path / "config"
        config_dir.mkdir()
        store = setup_two_projects(config_dir)
        store.set_project_branch("prod", 456, "feature-x")
        registry = ServiceRegistry.__new__(ServiceRegistry)
        registry.branch = _branch_service(store)

        app_ = create_app(config_dir=str(config_dir), auth_token="test-token")
        app_.dependency_overrides[get_registry] = lambda: registry

        with TestClient(app_) as client:
            response = client.get(
                "/branches/current",
                params={"project": "prod"},
                headers={"Authorization": "Bearer test-token"},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["active_count"] == 1
        assert body["projects"][0]["active_branch_name"] == "feature-x"
