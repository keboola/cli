"""CLI tests for `kbagent project create` (agent provisioning, DMD-1940).

The service is mocked throughout -- these tests pin the *command* layer's
contract: argument wiring, exit codes, permission classification, and the two
things that make this command different from every other one in the group.

First, it is the only kbagent command reachable with no Keboola identity at
all, so its failure mode on a stack without the `agent-provisioning` feature
has to read as an answer rather than a crash. Second, its result deliberately
carries one credential-bearing value -- the single-use confirmation link --
because nobody can ever own the provisioned project without it; the access and
refresh tokens must still never appear anywhere.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.permissions import OPERATION_REGISTRY
from keboola_agent_cli.services.auth_service import ProvisionProjectResult, RegisteredProject

STACK_URL = "https://connection.keboola.com"
CONFIRM_URL = "https://connection.keboola.com/agent-project/confirm?token=kbc_apc_7_secret"
SECRET_ACCESS_TOKEN = "kbc_at_should_never_leak_00000000"
SECRET_REFRESH_TOKEN = "kbc_rt_should_never_leak_00000000"

runner = CliRunner()


def _invoke(config_dir: Path, svc: MagicMock, args: list[str]):
    with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
        return runner.invoke(app, ["--config-dir", str(config_dir), *args])


def _result(**overrides: Any) -> ProvisionProjectResult:
    defaults: dict[str, Any] = {
        "status": "ok",
        "stack_url": STACK_URL,
        "project_id": 9840,
        "project_name": "Agent Project",
        "backend": "snowflake",
        "session_id": "sess-9",
        "access_expires_at": "2026-09-17T12:00:00+00:00",
        "confirm_url": CONFIRM_URL,
        "backend_init_dispatched_async": False,
        "registered_projects": [
            RegisteredProject(
                alias="agent-project",
                project_id=9840,
                project_name="Agent Project",
                status="registered",
            )
        ],
        "next_steps": [f"Open {CONFIRM_URL} and sign in.", "Then run `kbagent auth login`."],
        "warnings": [],
    }
    defaults.update(overrides)
    return ProvisionProjectResult(**defaults)  # type: ignore[arg-type]


class TestProjectCreate:
    def test_passes_every_option_to_the_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result()

        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "project",
                "create",
                "--url",
                STACK_URL,
                "--project",
                "scratch",
                "--name",
                "Agent Project",
                "--backend",
                "bigquery",
                "--sync-backend-init",
            ],
        )

        assert result.exit_code == 0
        svc.provision_project.assert_called_once_with(
            stack=STACK_URL,
            alias="scratch",
            project_name="Agent Project",
            backend="bigquery",
            sync_backend_init=True,
        )

    def test_defaults_leave_the_stack_to_decide(self, tmp_path: Path) -> None:
        """An omitted --backend must reach the service as None, not a guess:
        None is what keeps the stack maintainer's own default."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result()

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        assert result.exit_code == 0
        svc.provision_project.assert_called_once_with(
            stack=STACK_URL,
            alias="",
            project_name="",
            backend=None,
            sync_backend_init=False,
        )

    def test_url_is_required(self, tmp_path: Path) -> None:
        """Provisioning a billable project on a guessed stack is not a mistake
        worth being able to make."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()

        result = _invoke(config_dir, svc, ["project", "create"])

        assert result.exit_code == 2
        svc.provision_project.assert_not_called()

    def test_json_carries_the_confirm_url_and_no_tokens(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result()

        result = _invoke(config_dir, svc, ["--json", "project", "create", "--url", STACK_URL])

        payload = json.loads(result.stdout)["data"]
        assert payload["project_id"] == 9840
        assert payload["confirm_url"] == CONFIRM_URL
        assert SECRET_ACCESS_TOKEN not in result.stdout
        assert SECRET_REFRESH_TOKEN not in result.stdout
        assert "kbc_at_" not in result.stdout
        assert "kbc_rt_" not in result.stdout

    def test_human_output_prints_the_link_and_next_steps(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result()

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        assert result.exit_code == 0
        # Rich wraps a narrow terminal; assert on the unwrappable tail of the
        # link plus the ownership warning, not on the whole URL.
        assert "kbc_apc_7_secret" in result.stdout.replace("\n", "")
        assert "Nobody owns this project yet" in result.stdout
        assert "auth login" in result.stdout

    def test_url_is_not_taken_from_the_environment(self, tmp_path: Path, monkeypatch) -> None:
        """`project add` reads KBC_STORAGE_API_URL; this must not. An ambient
        value would quietly decide which stack pays for a billable project."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        monkeypatch.setenv("KBC_STORAGE_API_URL", STACK_URL)
        svc = MagicMock()

        result = _invoke(config_dir, svc, ["project", "create"])

        assert result.exit_code == 2
        svc.provision_project.assert_not_called()

    def test_confirm_url_markup_is_not_interpreted(self, tmp_path: Path) -> None:
        """The URL is server-supplied, so Rich markup in it must print
        literally -- and must not be backslash-escaped either, since the user
        copy-pastes the string verbatim."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        hostile = "https://connection.keboola.com/agent-project/confirm?token=[bold]x[/bold]"
        svc = MagicMock()
        svc.provision_project.return_value = _result(
            confirm_url=hostile,
            next_steps=[f"Open {hostile} and sign in.", "Then run `kbagent auth login`."],
        )

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        flat = result.stdout.replace("\n", "")
        assert "[bold]x[/bold]" in flat
        assert "\\[bold]" not in flat

    def test_warnings_are_shown(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result(
            backend_init_dispatched_async=True,
            warnings=["The project's storage backend is still being initialized."],
        )

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        assert "still being initialized" in result.stdout

    def test_panel_does_not_name_an_alias_that_was_not_registered(self, tmp_path: Path) -> None:
        """`apply_selections` echoes the REQUESTED alias on a skip, so reading
        the name alone printed "Local alias: taken" directly above a next step
        saying no alias was registered."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result(
            registered_projects=[
                RegisteredProject(
                    alias="taken",
                    project_id=9840,
                    project_name="Agent Project",
                    status="skipped",
                    note="Alias 'taken' already points at a different project.",
                )
            ],
            next_steps=["Open the link.", "No local alias was registered for project 9840."],
        )

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        flat = " ".join(result.stdout.split())
        assert "Local alias: none registered" in flat
        assert "Local alias: taken" not in flat

    def test_panel_names_a_registered_alias(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.return_value = _result()

        result = _invoke(config_dir, svc, ["project", "create", "--url", STACK_URL])

        assert "agent-project" in " ".join(result.stdout.split())

    def test_feature_off_is_an_answer_not_a_traceback(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.side_effect = KeboolaApiError(
            "Creating a Keboola project from the CLI is not enabled on this stack.",
            status_code=404,
            error_code=ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK,
            retryable=False,
        )

        result = _invoke(config_dir, svc, ["--json", "project", "create", "--url", STACK_URL])

        assert result.exit_code == 1
        payload = json.loads(result.stdout)
        assert payload["error"]["code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert "not enabled on this stack" in payload["error"]["message"]
        assert "Traceback" not in result.stdout

    def test_existing_session_is_a_config_error(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.provision_project.side_effect = ConfigError(
            "A Keboola session for this stack already exists."
        )

        result = _invoke(config_dir, svc, ["--json", "project", "create", "--url", STACK_URL])

        assert result.exit_code == 5
        assert json.loads(result.stdout)["error"]["code"] == ErrorCode.CONFIG_ERROR


class TestFeatureOffEndToEnd:
    """The full CLI path -- argv to rendered message -- with only the socket faked.

    Every other test in this file mocks `AuthService`, which is right for
    argument wiring but would not catch the thing that actually matters here:
    the command is ALWAYS registered, so on a stack without
    `STACK_FEATURES__AGENT_PROVISIONING` the 404 response is the only place a
    user ever learns the capability is missing. These drive the real service,
    the real client and the real error mapping so the message, the exit code
    and the absence of a traceback are pinned end to end.
    """

    def test_human_mode_explains_the_missing_stack_feature(
        self, tmp_path: Path, httpx_mock
    ) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        # A legacy dispatcher answers HTML, not JSON -- the message must not
        # degrade into markup or an unparsed-body error.
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/programmatic-projects",
            method="POST",
            status_code=404,
            html="<html><body><h1>404 Not Found</h1></body></html>",
        )

        result = runner.invoke(
            app, ["--config-dir", str(config_dir), "project", "create", "--url", STACK_URL]
        )

        assert result.exit_code == 1, result.output
        flat = " ".join(result.output.split())
        assert "not available on" in flat
        assert "agent-provisioning" in flat
        assert "STACK_FEATURES__AGENT_PROVISIONING" in flat
        # Names a way forward, and never a stack trace.
        assert "kbagent auth login" in flat
        assert "Traceback" not in result.output
        assert "404 Not Found" not in result.output

    def test_json_mode_carries_the_code_and_message(self, tmp_path: Path, httpx_mock) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/programmatic-projects",
            method="POST",
            status_code=404,
            json={"error": "Not Found"},
        )

        result = runner.invoke(
            app,
            ["--json", "--config-dir", str(config_dir), "project", "create", "--url", STACK_URL],
        )

        assert result.exit_code == 1, result.output
        error = json.loads(result.stdout)["error"]
        assert error["code"] == ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK
        assert "STACK_FEATURES__AGENT_PROVISIONING" in error["message"]
        assert error["retryable"] is False

    def test_nothing_is_persisted_when_the_feature_is_off(self, tmp_path: Path, httpx_mock) -> None:
        """No session in auth.json, no alias in config.json, no stray files."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/programmatic-projects",
            method="POST",
            status_code=404,
            json={"error": "Not Found"},
        )

        runner.invoke(
            app, ["--config-dir", str(config_dir), "project", "create", "--url", STACK_URL]
        )

        assert not (config_dir / "auth.json").exists()
        config = config_dir / "config.json"
        assert not config.exists() or json.loads(config.read_text())["projects"] == {}

    def test_the_feature_check_costs_exactly_one_request(self, tmp_path: Path, httpx_mock) -> None:
        """A disabled feature never becomes enabled by retrying, and the call
        is not idempotent -- so a 404 must not be retried."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        httpx_mock.add_response(
            url=f"{STACK_URL}/manage/programmatic-projects",
            method="POST",
            status_code=404,
            json={"error": "Not Found"},
        )

        runner.invoke(
            app, ["--config-dir", str(config_dir), "project", "create", "--url", STACK_URL]
        )

        assert len(httpx_mock.get_requests()) == 1


class TestPermissionClassification:
    def test_create_is_admin_class(self) -> None:
        """It provisions a real organization, project and credit grant."""
        assert OPERATION_REGISTRY["project.create"] == "admin"

    def test_deny_writes_blocks_it(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()

        result = _invoke(
            config_dir, svc, ["--deny-writes", "--json", "project", "create", "--url", STACK_URL]
        )

        assert result.exit_code == EXIT_PERMISSION_DENIED
        svc.provision_project.assert_not_called()
