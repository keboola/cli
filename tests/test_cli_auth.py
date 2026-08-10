"""CLI tests for the `kbagent auth` command group (login / status / logout /
register-projects).

The service is mocked throughout -- these tests pin the *command* layer's
contract: argument wiring, exit codes, permission classification, and the
hard requirement that no token substring ever reaches `--json` output.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.config_store import CURRENT_CONFIG_VERSION
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.errors import ConfigError, ErrorCode, KeboolaApiError
from keboola_agent_cli.permissions import OPERATION_REGISTRY
from keboola_agent_cli.services.auth_service import (
    SESSION_UNSUPPORTED_FEATURES,
    AuthStatusResult,
    LoginResult,
    LogoutResult,
    ProjectCandidate,
    ProjectCandidatesResult,
    RegisteredProject,
    RegisterProjectsResult,
)

STACK_URL = "https://connection.keboola.com"
SECRET_ACCESS_TOKEN = "kbc_at_should_never_leak_00000000"
SECRET_REFRESH_TOKEN = "kbc_rt_should_never_leak_00000000"

runner = CliRunner()


def _invoke(config_dir: Path, svc: MagicMock, args: list[str], input_text: str | None = None):
    with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
        return runner.invoke(app, ["--config-dir", str(config_dir), *args], input=input_text)


def _login_result(**overrides: Any) -> LoginResult:
    defaults: dict[str, Any] = {
        "status": "ok",
        "method": "device",
        "stack_url": STACK_URL,
        "session_id": "sess-1",
        "user_email": "user@example.com",
        "user_name": "Test User",
        "access_expires_at": "2026-07-27T12:00:00+00:00",
        "refresh_expires_at": "2026-08-26T12:00:00+00:00",
        "fallback_reason": "",
        "replaced_session_id": "",
        "orphaned_session_id": "",
        "accessible_projects": [{"id": 101, "name": "Prod Project", "role": "admin"}],
        "registered_projects": [],
        "warnings": [],
    }
    defaults.update(overrides)
    return LoginResult(**defaults)  # type: ignore[arg-type]


def _status_result(**overrides: Any) -> AuthStatusResult:
    defaults: dict[str, Any] = {
        "status": "live",
        "stack_url": STACK_URL,
        "session_id": "sess-1",
        "user_email": "user@example.com",
        "user_name": "Test User",
        "access_expires_at": "2026-07-27T12:00:00+00:00",
        "refresh_expires_at": "2026-08-26T12:00:00+00:00",
        "accessible_projects": [{"id": 101, "name": "Prod Project", "role": "admin"}],
        "orphaned_session_ids": [],
        "detail": "",
    }
    defaults.update(overrides)
    return AuthStatusResult(**defaults)  # type: ignore[arg-type]


def _logout_result(**overrides: Any) -> LogoutResult:
    defaults: dict[str, Any] = {
        "status": "ok",
        "stack_url": STACK_URL,
        "session_id": "sess-1",
        "remote_revoked": True,
        "detail": "",
        "removed_projects": [],
        "orphans_revoked": [],
        "orphans_remaining": [],
    }
    defaults.update(overrides)
    return LogoutResult(**defaults)  # type: ignore[arg-type]


def _candidate(**overrides: Any) -> ProjectCandidate:
    defaults: dict[str, Any] = {
        "project_id": 9840,
        "project_name": "Jirka BQ SOX",
        "role": "admin",
        "default_alias": "jirka-bq-sox",
        "existing_alias": "",
        "registered": False,
    }
    defaults.update(overrides)
    return ProjectCandidate(**defaults)  # type: ignore[arg-type]


def _register_result(**overrides: Any) -> RegisterProjectsResult:
    defaults: dict[str, Any] = {
        "status": "ok",
        "stack_url": STACK_URL,
        "registered_projects": [],
        "warnings": [],
    }
    defaults.update(overrides)
    return RegisterProjectsResult(**defaults)  # type: ignore[arg-type]


class TestLogin:
    def test_device_code_json(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        result = _invoke(
            config_dir,
            svc,
            ["--json", "auth", "login", "--stack", STACK_URL, "--device-code"],
        )
        assert result.exit_code == 0, result.output
        # stdout must stay a SINGLE valid JSON document even though the
        # post-login hook writes a hint to stderr (accessible_projects is
        # non-empty and --register-projects was not passed) -- parse
        # result.stdout specifically, not the mixed result.output.
        data = json.loads(result.stdout)["data"]
        assert data["method"] == "device"
        assert data["session_id"] == "sess-1"
        kwargs = svc.login.call_args.kwargs
        assert kwargs["stack"] == STACK_URL
        assert kwargs["device_code"] is True
        assert kwargs["register_projects"] is False
        assert callable(kwargs["on_device_prompt"])
        assert callable(kwargs["on_notice"])

    def test_register_projects_flag_forwarded(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        result = _invoke(
            config_dir,
            svc,
            ["auth", "login", "--device-code", "--register-projects"],
        )
        assert result.exit_code == 0, result.output
        assert svc.login.call_args.kwargs["register_projects"] is True

    def test_json_output_never_contains_a_token_substring(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        # The service could not possibly return a token (LoginResult has no such
        # field) -- this test pins that no code path in the command layer prints
        # one either, by asserting the well-known secret substrings never appear.
        svc.login.return_value = _login_result()
        result = _invoke(config_dir, svc, ["--json", "auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert SECRET_ACCESS_TOKEN not in result.output
        assert SECRET_REFRESH_TOKEN not in result.output
        assert "access_token" not in result.output
        assert "refresh_token" not in result.output
        # stdout alone must be exactly one JSON document (see comment above).
        data = json.loads(result.stdout)["data"]
        assert set(data.keys()).isdisjoint({"access_token", "refresh_token", "token"})

    def test_config_error_maps_to_exit_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()

        svc = MagicMock()
        svc.login.side_effect = ConfigError("No stack to log into.")
        result = _invoke(config_dir, svc, ["--json", "auth", "login"])
        assert result.exit_code == 5
        assert json.loads(result.output)["status"] == "error"

    def test_not_supported_on_stack_404_reports_clear_message(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.side_effect = KeboolaApiError(
            "Browser login is not enabled on this Keboola stack yet.",
            status_code=404,
            error_code=ErrorCode.AUTH_NOT_SUPPORTED_ON_STACK,
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "login", "--stack", STACK_URL])
        assert result.exit_code != 0
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert body["error"]["code"] == "AUTH_NOT_SUPPORTED_ON_STACK"

    def test_state_mismatch_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.side_effect = KeboolaApiError(
            "state mismatch", error_code=ErrorCode.AUTH_STATE_MISMATCH
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "login"])
        assert result.exit_code != 0
        assert json.loads(result.output)["error"]["code"] == "AUTH_STATE_MISMATCH"


class TestLoginPassword:
    def test_success_forwards_args_and_computed_totp_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login_password.return_value = _login_result(method="password")
        result = _invoke(
            config_dir,
            svc,
            [
                "auth",
                "login-password",
                "--email",
                "svc@example.com",
                "--password",
                "s3cr3t",
                "--totp-secret",
                "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = svc.login_password.call_args.kwargs
        assert kwargs["email"] == "svc@example.com"
        assert kwargs["password"] == "s3cr3t"
        assert kwargs["totp_code"] is not None
        assert kwargs["totp_code"].isdigit()
        assert len(kwargs["totp_code"]) == 6

    def test_no_totp_secret_passes_none(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login_password.return_value = _login_result(method="password")
        result = _invoke(
            config_dir,
            svc,
            ["auth", "login-password", "--email", "svc@example.com", "--password", "s3cr3t"],
        )
        assert result.exit_code == 0, result.output
        assert svc.login_password.call_args.kwargs["totp_code"] is None

    def test_env_vars_populate_email_and_password(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login_password.return_value = _login_result(method="password")
        with (
            patch("keboola_agent_cli.cli.AuthService", return_value=svc),
            patch.dict(
                "os.environ",
                {"KBC_LOGIN_EMAIL": "svc@example.com", "KBC_LOGIN_PASSWORD": "s3cr3t"},
            ),
        ):
            result = runner.invoke(app, ["--config-dir", str(config_dir), "auth", "login-password"])
        assert result.exit_code == 0, result.output
        kwargs = svc.login_password.call_args.kwargs
        assert kwargs["email"] == "svc@example.com"
        assert kwargs["password"] == "s3cr3t"

    def test_mfa_invalid_error_surfaces(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login_password.side_effect = KeboolaApiError(
            "webauthn-only account", error_code=ErrorCode.AUTH_MFA_INVALID
        )
        result = _invoke(
            config_dir,
            svc,
            ["--json", "auth", "login-password", "--email", "e", "--password", "p"],
        )
        assert result.exit_code != 0
        assert json.loads(result.stdout)["error"]["code"] == "AUTH_MFA_INVALID"

    def test_password_never_appears_in_output(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login_password.return_value = _login_result(method="password")
        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "auth",
                "login-password",
                "--email",
                "svc@example.com",
                "--password",
                "SuperSecretPassword123",
            ],
        )
        assert "SuperSecretPassword123" not in result.output


class TestPostLoginHook:
    """The optional "register these projects now?" nudge after a plain login."""

    def test_json_mode_prints_hint_to_stderr_only(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        result = _invoke(config_dir, svc, ["--json", "auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert "register-projects" in result.stderr
        assert "register-projects" not in result.stdout
        # stdout is still exactly one JSON document.
        json.loads(result.stdout)
        svc.register_projects.assert_not_called()

    def test_non_tty_human_mode_prints_hint_and_exits_zero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        # CliRunner's captured stdout is never a TTY, so the hook takes the
        # same "print hint only" branch as --json without needing to patch
        # anything -- this pins that behaviour explicitly.
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert "Run 'kbagent auth register-projects'" in result.output
        svc.register_projects.assert_not_called()

    def test_no_accessible_projects_skips_hook_entirely(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(accessible_projects=[])
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert "register-projects" not in result.output

    def test_register_projects_flag_skips_hook_entirely(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code", "--register-projects"])
        assert result.exit_code == 0, result.output
        assert "register-projects" not in result.output
        svc.candidates_from_projects.assert_not_called()

    def test_tty_declined_prints_hint_and_exits_zero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(config_dir, svc, ["auth", "login", "--device-code"], input_text="n\n")
        assert result.exit_code == 0, result.output
        assert "Run 'kbagent auth register-projects'" in result.output
        svc.register_projects.assert_not_called()

    def test_tty_accepted_drives_picker_and_registers(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(
            accessible_projects=[
                {"id": 9840, "name": "Jirka BQ SOX", "role": "admin"},
                {"id": 1234, "name": "Demo", "role": "guest"},
            ]
        )
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox"),
            _candidate(project_id=1234, project_name="Demo", default_alias="demo"),
        ]
        svc.candidates_from_projects.return_value = candidates
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="jirka-bq-sox",
                    project_id=9840,
                    project_name="Jirka BQ SOX",
                    status="registered",
                )
            ]
        )
        # confirm(yes) -> select "1" -> accept default alias -> confirm(yes).
        input_text = "y\n1\n\n\n"
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(
                config_dir, svc, ["auth", "login", "--device-code"], input_text=input_text
            )
        assert result.exit_code == 0, result.output
        assert "jirka-bq-sox" in result.output
        svc.register_projects.assert_called_once()
        selections = svc.register_projects.call_args.kwargs["selections"]
        assert [s.project_id for s in selections] == [9840]

    def test_bare_enter_at_selection_prompt_registers_all(self, tmp_path: Path) -> None:
        """Regression for the picker's selection-prompt default (`all`, not `none`).

        Two bare Enters (accept the hook's "register now?" question, then
        accept the selection prompt's default) must register EVERY
        accessible project, not zero -- that was the whole point of
        changing the picker's default away from `none`. Asserts on the
        actual registered project ids, not just exit code, so flipping the
        default back to `none` fails this test.
        """
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(
            accessible_projects=[
                {"id": 9840, "name": "Jirka BQ SOX", "role": "admin"},
                {"id": 1234, "name": "Demo", "role": "guest"},
            ]
        )
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox"),
            _candidate(project_id=1234, project_name="Demo", default_alias="demo"),
        ]
        svc.candidates_from_projects.return_value = candidates
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="jirka-bq-sox",
                    project_id=9840,
                    project_name="Jirka BQ SOX",
                    status="registered",
                ),
                RegisteredProject(
                    alias="demo", project_id=1234, project_name="Demo", status="registered"
                ),
            ]
        )
        # Bare Enter x5: accept "register now?" -> accept selection default
        # ("all") -> accept both suggested aliases -> accept final confirm.
        input_text = "\n\n\n\n\n"
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(
                config_dir, svc, ["auth", "login", "--device-code"], input_text=input_text
            )
        assert result.exit_code == 0, result.output
        svc.register_projects.assert_called_once()
        selections = svc.register_projects.call_args.kwargs["selections"]
        assert {s.project_id for s in selections} == {9840, 1234}

    def test_explicit_none_at_selection_prompt_still_registers_nothing(
        self, tmp_path: Path
    ) -> None:
        """The `all` default must not remove the `none` escape hatch."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(
            accessible_projects=[{"id": 9840, "name": "Jirka BQ SOX", "role": "admin"}]
        )
        svc.candidates_from_projects.return_value = [_candidate(project_id=9840)]
        # Accept "register now?" -> explicitly type "none" at the selection prompt.
        input_text = "\nnone\n"
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(
                config_dir, svc, ["auth", "login", "--device-code"], input_text=input_text
            )
        assert result.exit_code == 0, result.output
        svc.register_projects.assert_not_called()

    def test_hook_failure_does_not_change_exit_code(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result()
        svc.candidates_from_projects.side_effect = KeboolaApiError(
            "offline", error_code=ErrorCode.CONNECTION_ERROR
        )
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(config_dir, svc, ["auth", "login", "--device-code"], input_text="y\n")
        # Login already succeeded and was already reported -- a failure in
        # this optional follow-up must never flip a successful login to a
        # non-zero exit.
        assert result.exit_code == 0, result.output
        assert "Warning" in result.output


class TestStatus:
    def test_live_exits_zero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(status="live")
        result = _invoke(config_dir, svc, ["--json", "auth", "status"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["status"] == "live"

    def test_refreshed_exits_zero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(status="refreshed")
        result = _invoke(config_dir, svc, ["--json", "auth", "status"])
        assert result.exit_code == 0, result.output

    def test_degraded_exits_zero(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(status="degraded", detail="offline")
        result = _invoke(config_dir, svc, ["--json", "auth", "status"])
        assert result.exit_code == 0, result.output

    def test_expired_exits_3(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(status="expired", detail="expired")
        result = _invoke(config_dir, svc, ["--json", "auth", "status"])
        assert result.exit_code == 3, result.output
        # The command still prints the (informative) result before exiting non-zero.
        assert json.loads(result.output)["data"]["status"] == "expired"

    def test_missing_exits_3(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(
            status="missing", session_id="", accessible_projects=[]
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "status"])
        assert result.exit_code == 3, result.output

    def test_stack_option_forwarded(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result()
        _invoke(config_dir, svc, ["--json", "auth", "status", "--stack", "myalias"])
        svc.status.assert_called_once_with(stack="myalias")


class TestLogout:
    def test_confirmed_revoke(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.logout.return_value = _logout_result(remote_revoked=True)
        result = _invoke(config_dir, svc, ["auth", "logout", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Signed out" in result.output

    def test_uncertain_revoke_reported_distinctly(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.logout.return_value = _logout_result(
            remote_revoked=False, detail="server session sess-1 may still be active"
        )
        result = _invoke(config_dir, svc, ["auth", "logout", "--yes"])
        assert result.exit_code == 0, result.output
        assert "may still be active" in result.output

    def test_confirm_abort(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        result = _invoke(config_dir, svc, ["auth", "logout"], input_text="n\n")
        assert result.exit_code == 0
        assert "Aborted" in result.output
        svc.logout.assert_not_called()

    def test_remove_projects_flag_forwarded(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.logout.return_value = _logout_result(removed_projects=["padak"])
        result = _invoke(config_dir, svc, ["auth", "logout", "--yes", "--remove-projects"])
        assert result.exit_code == 0, result.output
        svc.logout.assert_called_once_with(stack=None, remove_projects=True)

    def test_session_not_found_exit_3(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.logout.side_effect = KeboolaApiError(
            "No active Keboola session.", error_code=ErrorCode.SESSION_NOT_FOUND
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "logout", "--yes"])
        assert result.exit_code == 3, result.output


class TestRegisterProjects:
    def test_all_delegates_selection_to_the_service(self, tmp_path: Path) -> None:
        """`--all` hands `select_all=True` over; the command never builds the list.

        Selecting every accessible project needs the accessible set, which only
        the service can fetch -- so the command must not pre-fetch candidates
        just to turn them into selections.
        """
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="jirka-bq-sox",
                    project_id=9840,
                    project_name="Jirka BQ SOX",
                    status="registered",
                ),
                RegisteredProject(
                    alias="demo", project_id=1234, project_name="Demo", status="registered"
                ),
            ]
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "register-projects", "--all"])
        assert result.exit_code == 0, result.output
        kwargs = svc.register_projects.call_args.kwargs
        assert kwargs["select_all"] is True
        assert kwargs["project_ids"] is None
        assert kwargs["alias_overrides"] == {}
        assert "selections" not in kwargs
        svc.list_project_candidates.assert_not_called()

    def test_project_id_repeatable(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result()
        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "auth",
                "register-projects",
                "--project-id",
                "9840",
                "--project-id",
                "1234",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = svc.register_projects.call_args.kwargs
        assert kwargs["project_ids"] == [9840, 1234]
        assert kwargs["select_all"] is False
        # --project-id must not trigger a redundant introspection round trip --
        # `register_projects` already introspects and validates internally.
        svc.list_project_candidates.assert_not_called()

    def test_all_and_project_id_together_exit_2(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        result = _invoke(
            config_dir,
            svc,
            ["--json", "auth", "register-projects", "--all", "--project-id", "9840"],
        )
        assert result.exit_code == 2, result.output
        svc.register_projects.assert_not_called()

    def test_no_selector_non_tty_raises_config_error(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        # CliRunner's stdout is never a TTY, so no patch is needed to hit
        # this branch: the picker requires a real terminal.
        result = _invoke(config_dir, svc, ["--json", "auth", "register-projects"])
        assert result.exit_code == 5, result.output
        body = json.loads(result.output)
        assert body["status"] == "error"
        assert "--all or --project-id" in body["error"]["message"]
        svc.register_projects.assert_not_called()

    def test_alias_override_takes_effect(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result()
        result = _invoke(
            config_dir,
            svc,
            [
                "--json",
                "auth",
                "register-projects",
                "--project-id",
                "9840",
                "--alias",
                "9840=my-alias",
            ],
        )
        assert result.exit_code == 0, result.output
        kwargs = svc.register_projects.call_args.kwargs
        assert kwargs["project_ids"] == [9840]
        assert kwargs["alias_overrides"] == {9840: "my-alias"}

    def test_invalid_alias_override_exits_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        result = _invoke(
            config_dir,
            svc,
            ["--json", "auth", "register-projects", "--project-id", "9840", "--alias", "bad"],
        )
        assert result.exit_code == 5, result.output
        svc.register_projects.assert_not_called()
        svc.list_project_candidates.assert_not_called()

    def test_interactive_picker_selects_subset_with_default_alias(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox"),
            _candidate(project_id=1234, project_name="Demo", default_alias="demo"),
        ]
        svc.list_project_candidates.return_value = ProjectCandidatesResult(
            stack_url=STACK_URL, candidates=candidates
        )
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="jirka-bq-sox",
                    project_id=9840,
                    project_name="Jirka BQ SOX",
                    status="registered",
                )
            ]
        )
        # Select "1" -> accept the suggested default alias (bare Enter) ->
        # accept the final confirmation (bare Enter, default=True).
        input_text = "1\n\n\n"
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(config_dir, svc, ["auth", "register-projects"], input_text=input_text)
        assert result.exit_code == 0, result.output
        selections = svc.register_projects.call_args.kwargs["selections"]
        assert len(selections) == 1
        assert selections[0].project_id == 9840
        assert selections[0].alias == "jirka-bq-sox"
        assert "jirka-bq-sox" in result.output

    def test_bare_enter_at_selection_prompt_registers_all(self, tmp_path: Path) -> None:
        """Regression for the picker's selection-prompt default (`all`, not `none`).

        A bare Enter at "Select projects to register" must register EVERY
        candidate, not zero. Asserts on the actual registered project ids
        (not just exit code) so a regression back to `default="none"` in
        `_auth_picker._prompt_selection` fails this test.
        """
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        candidates = [
            _candidate(project_id=9840, project_name="Jirka BQ SOX", default_alias="jirka-bq-sox"),
            _candidate(project_id=1234, project_name="Demo", default_alias="demo"),
        ]
        svc.list_project_candidates.return_value = ProjectCandidatesResult(
            stack_url=STACK_URL, candidates=candidates
        )
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="jirka-bq-sox",
                    project_id=9840,
                    project_name="Jirka BQ SOX",
                    status="registered",
                ),
                RegisteredProject(
                    alias="demo", project_id=1234, project_name="Demo", status="registered"
                ),
            ]
        )
        # Bare Enter x4: accept selection default ("all") -> accept both
        # suggested aliases -> accept the final confirmation.
        input_text = "\n\n\n\n"
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(config_dir, svc, ["auth", "register-projects"], input_text=input_text)
        assert result.exit_code == 0, result.output
        selections = svc.register_projects.call_args.kwargs["selections"]
        assert {s.project_id for s in selections} == {9840, 1234}

    def test_interactive_picker_empty_selection_exits_zero_without_registering(
        self, tmp_path: Path
    ) -> None:
        """The `all` default must not remove the `none` escape hatch."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        candidates = [_candidate()]
        svc.list_project_candidates.return_value = ProjectCandidatesResult(
            stack_url=STACK_URL, candidates=candidates
        )
        with patch("keboola_agent_cli.commands.auth._is_stdout_tty", return_value=True):
            result = _invoke(config_dir, svc, ["auth", "register-projects"], input_text="none\n")
        assert result.exit_code == 0, result.output
        svc.register_projects.assert_not_called()

    def test_unknown_project_id_config_error_from_service(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.side_effect = ConfigError(
            "Project 9999 is not accessible to the current session."
        )
        result = _invoke(
            config_dir, svc, ["--json", "auth", "register-projects", "--project-id", "9999"]
        )
        assert result.exit_code == 5, result.output


class TestPermissionClassification:
    def test_registry_entries(self) -> None:
        assert OPERATION_REGISTRY["auth.login"] == "write"
        assert OPERATION_REGISTRY["auth.logout"] == "write"
        assert OPERATION_REGISTRY["auth.status"] == "read"
        assert OPERATION_REGISTRY["auth.register-projects"] == "write"

    def test_deny_writes_blocks_login(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--deny-writes",
                    "--json",
                    "auth",
                    "login",
                    "--device-code",
                ],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        svc.login.assert_not_called()

    def test_deny_writes_blocks_register_projects(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
            result = runner.invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--deny-writes",
                    "--json",
                    "auth",
                    "register-projects",
                    "--all",
                ],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        svc.register_projects.assert_not_called()

    def test_remove_projects_needs_the_admin_class(self, tmp_path: Path) -> None:
        """`--remove-projects` deletes config.json entries, so cli:admin gates it.

        The bare logout stays available: denying admin must not stop an agent
        from ending its own session.
        """
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        base = ["--config-dir", str(config_dir)]
        # Written directly: `permissions set` requires a human to type a
        # confirmation code at a real terminal and has no --yes bypass.
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "version": CURRENT_CONFIG_VERSION,
                    "projects": {},
                    "permissions": {"mode": "allow", "allow": [], "deny": ["cli:admin"]},
                }
            )
        )

        svc = MagicMock()
        svc.logout.return_value = _logout_result()
        with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
            denied = runner.invoke(
                app, [*base, "--json", "auth", "logout", "--yes", "--remove-projects"]
            )
        assert denied.exit_code == EXIT_PERMISSION_DENIED
        svc.logout.assert_not_called()

        svc = MagicMock()
        svc.logout.return_value = _logout_result()
        with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
            allowed = runner.invoke(app, [*base, "--json", "auth", "logout", "--yes"])
        assert allowed.exit_code == 0, allowed.output
        svc.logout.assert_called_once()

    def test_deny_writes_does_not_block_status(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result()
        with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
            result = runner.invoke(
                app,
                ["--config-dir", str(config_dir), "--deny-writes", "--json", "auth", "status"],
            )
        assert result.exit_code == 0, result.output
        svc.status.assert_called_once()


class TestHelp:
    def test_auth_help_renders(self, tmp_path: Path) -> None:
        result = runner.invoke(app, ["auth", "--help"])
        assert result.exit_code == 0
        assert "login" in result.output
        assert "status" in result.output
        assert "logout" in result.output
        assert "register-projects" in result.output

    def test_login_help_does_not_name_a_private_symbol(self, tmp_path: Path) -> None:
        """Help text is user-facing: it must point at a runnable command."""
        result = runner.invoke(app, ["auth", "login", "--help"])
        assert result.exit_code == 0
        assert "_run_post_login_hook" not in result.output


class TestServerControlledStringsAreNotMarkup:
    """Project / user strings come from the stack, so they must render literally.

    `OutputFormatter`'s Console has `markup=True`, and a project name is
    settable by anyone with rename rights on a shared project -- an unescaped
    `[link=...]` name would render as a clickable, deceptively-labelled
    hyperlink in another admin's terminal during their own `auth status` run.
    """

    HOSTILE = "[link=https://phish.example]Payroll[/link]"

    def test_status_accessible_projects_table_escapes_name_and_role(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(
            accessible_projects=[{"id": 101, "name": self.HOSTILE, "role": "[blink]admin"}]
        )
        result = _invoke(config_dir, svc, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert self.HOSTILE in result.output
        assert "[blink]admin" in result.output

    def test_status_panel_escapes_the_user_name(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.status.return_value = _status_result(user_name="[red]Not Really Admin[/red]")
        result = _invoke(config_dir, svc, ["auth", "status"])
        assert result.exit_code == 0, result.output
        assert "[red]Not Really Admin[/red]" in result.output

    def test_login_panel_escapes_the_user_name(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(
            user_name="[red]Not Really Admin[/red]", accessible_projects=[]
        )
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert "[red]Not Really Admin[/red]" in result.output

    def test_registered_projects_table_escapes_the_project_name(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result(
            registered_projects=[
                RegisteredProject(
                    alias="payroll", project_id=9840, project_name=self.HOSTILE, status="registered"
                )
            ]
        )
        result = _invoke(config_dir, svc, ["auth", "register-projects", "--all"])
        assert result.exit_code == 0, result.output
        assert self.HOSTILE in result.output

    def test_logout_detail_from_the_server_is_escaped(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.logout.return_value = _logout_result(
            remote_revoked=False, detail="server said [red]nope[/red]"
        )
        result = _invoke(config_dir, svc, ["auth", "logout", "--yes"])
        assert result.exit_code == 0, result.output
        assert "[red]nope[/red]" in result.output


class TestSessionRestrictionDisclosure:
    """The v1 restrictions must be stated at registration time, not on first failure."""

    def _registered(self) -> list[RegisteredProject]:
        return [
            RegisteredProject(
                alias="payroll", project_id=9840, project_name="Payroll", status="registered"
            )
        ]

    def test_register_projects_human_output_lists_the_restrictions(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result(
            registered_projects=self._registered()
        )
        result = _invoke(config_dir, svc, ["auth", "register-projects", "--all"])
        assert result.exit_code == 0, result.output
        assert "Not available on session-backed projects" in result.output
        assert "kbagent kai" in result.output

    def test_register_projects_json_carries_the_list(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result(
            registered_projects=self._registered()
        )
        result = _invoke(config_dir, svc, ["--json", "auth", "register-projects", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["session_unsupported_features"] == list(SESSION_UNSUPPORTED_FEATURES)

    def test_nothing_registered_prints_no_restriction_panel(self, tmp_path: Path) -> None:
        """Nothing was registered, so there is no session project to warn about."""
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.register_projects.return_value = _register_result(registered_projects=[])
        result = _invoke(config_dir, svc, ["auth", "register-projects", "--all"])
        assert result.exit_code == 0, result.output
        assert "Not available on session-backed projects" not in result.output

    def test_login_with_register_projects_lists_the_restrictions(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(registered_projects=self._registered())
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code", "--register-projects"])
        assert result.exit_code == 0, result.output
        assert "Not available on session-backed projects" in result.output

    def test_login_json_carries_the_list(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(registered_projects=self._registered())
        result = _invoke(
            config_dir, svc, ["--json", "auth", "login", "--device-code", "--register-projects"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.stdout)["data"]
        assert data["session_unsupported_features"] == list(SESSION_UNSUPPORTED_FEATURES)

    def test_plain_login_without_registering_prints_no_panel(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        svc = MagicMock()
        svc.login.return_value = _login_result(accessible_projects=[])
        result = _invoke(config_dir, svc, ["auth", "login", "--device-code"])
        assert result.exit_code == 0, result.output
        assert "Not available on session-backed projects" not in result.output
