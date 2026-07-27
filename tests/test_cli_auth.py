"""CLI tests for the `kbagent auth` command group (login / status / logout).

The service is mocked throughout -- these tests pin the *command* layer's
contract: argument wiring, exit codes, permission classification, and the
hard requirement that no token substring ever reaches `--json` output.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from keboola_agent_cli.cli import app
from keboola_agent_cli.constants import EXIT_PERMISSION_DENIED
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.permissions import OPERATION_REGISTRY
from keboola_agent_cli.services.auth_service import AuthStatusResult, LoginResult, LogoutResult

STACK_URL = "https://connection.keboola.com"
SECRET_ACCESS_TOKEN = "kbc_at_should_never_leak_00000000"
SECRET_REFRESH_TOKEN = "kbc_rt_should_never_leak_00000000"

runner = CliRunner()


def _invoke(config_dir: Path, svc: MagicMock, args: list[str], input_text: str | None = None):
    with patch("keboola_agent_cli.cli.AuthService", return_value=svc):
        return runner.invoke(app, ["--config-dir", str(config_dir), *args], input=input_text)


def _login_result(**overrides: object) -> LoginResult:
    defaults: dict[str, object] = {
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


def _status_result(**overrides: object) -> AuthStatusResult:
    defaults: dict[str, object] = {
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


def _logout_result(**overrides: object) -> LogoutResult:
    defaults: dict[str, object] = {
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
        data = json.loads(result.output)["data"]
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
        data = json.loads(result.output)["data"]
        assert set(data.keys()).isdisjoint({"access_token", "refresh_token", "token"})

    def test_config_error_maps_to_exit_5(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        from keboola_agent_cli.errors import ConfigError

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


class TestPermissionClassification:
    def test_registry_entries(self) -> None:
        assert OPERATION_REGISTRY["auth.login"] == "write"
        assert OPERATION_REGISTRY["auth.logout"] == "write"
        assert OPERATION_REGISTRY["auth.status"] == "read"

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
